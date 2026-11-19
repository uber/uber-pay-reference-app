"""Versioned declarative wire scenarios, not Python test-name dispatch or eval.

Values exactly equal to ${binding.path} preserve JSON types; interpolated paths
are strings. Faults are allowlisted, namespace scoped, and always cleaned up.
"""

import copy
import json
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.parse import urlencode, urlsplit

from psycopg import sql

from . import contract


def field(value, path):
    for key in path.split(".") if path else []:
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def expand(value, bindings):
    if isinstance(value, str):
        match = re.fullmatch(r"\$\{([^}]+)\}", value)
        if match:
            return copy.deepcopy(field(bindings, match[1]))
        return re.sub(r"\$\{([^}]+)\}", lambda m: str(field(bindings, m[1])), value)
    if isinstance(value, list):
        return [expand(x, bindings) for x in value]
    if isinstance(value, dict):
        return {k: expand(v, bindings) for k, v in value.items()}
    return value


def assign(value, path, new):
    keys = path.split(".")
    for key in keys[:-1]:
        value = value[int(key)] if isinstance(value, list) else value[key]
    if isinstance(value, list):
        value[int(keys[-1])] = new
    else:
        value[keys[-1]] = new


class Scenario:
    def __init__(self, suite, bindings=None):
        self.suite = suite
        self.bindings = dict(
            id=suite.uid(), client_id=suite.client_id, client_secret=suite.client_secret
        ) | (bindings or {})

    @contextmanager
    def fault(self, table, code):
        if table not in {
            "accounts",
            "transactions",
            "operations",
            "jobs",
        } or code not in {"40001", "55P03"}:
            raise ValueError("Unapproved fault")
        name = "scenario_" + self.suite.uid()
        with self.suite.db() as db:
            db.execute(
                sql.SQL(
                    "CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.client_id={} THEN RAISE EXCEPTION 'scoped conformance fault' USING ERRCODE={}; END IF; RETURN NEW; END $$"
                ).format(
                    sql.Identifier(name),
                    sql.Literal(self.suite.client_id),
                    sql.Literal(code),
                )
            )
            try:
                db.execute(
                    sql.SQL(
                        "CREATE TRIGGER {} BEFORE INSERT OR UPDATE ON {} FOR EACH ROW EXECUTE FUNCTION {}()"
                    ).format(
                        sql.Identifier(name),
                        sql.Identifier(table),
                        sql.Identifier(name),
                    )
                )
                yield
            finally:
                db.execute(
                    sql.SQL("DROP TRIGGER IF EXISTS {} ON {}").format(
                        sql.Identifier(name), sql.Identifier(table)
                    )
                )
                db.execute(
                    sql.SQL("DROP FUNCTION IF EXISTS {}()").format(sql.Identifier(name))
                )

    def run(self, scenario):
        if scenario.get("version", 1) != 1:
            raise ValueError("Unsupported scenario version")
        for name, statuses in scenario.get("callback_faults", {}).items():
            self.suite.mock.fail_next(name, self.bindings["id"], *statuses)
        for step in scenario["steps"]:
            self.step(step)
        return self.bindings

    def assertions(self, reply, step):
        expected = expand(step.get("expect", {}), self.bindings)
        self.suite.assertEqual(reply.status, expected["status"], reply.raw[:500])
        template = step.get("contract_path")
        if template:
            contract.response(
                step.get("method", "post").lower(),
                template,
                reply.status,
                reply.body,
                grant=step.get("grant"),
            )
        for path, value in expected.get("body", {}).items():
            self.suite.assertEqual(field(reply.body, path), value)
        for name, value in expected.get("headers", {}).items():
            self.suite.assertEqual(reply.headers.get(name.lower()), value)
        for text in expected.get("contains", []):
            self.suite.assertIn(text, reply.raw.decode())
        for text in expected.get("absent", []):
            self.suite.assertNotIn(text, reply.raw.decode())
        for name, path in step.get("bind", {}).items():
            self.bindings[name] = field(reply.body, path)
        for name, path in step.get("bind_headers", {}).items():
            self.bindings[name] = reply.headers[path.lower()]
        if step.get("save"):
            self.bindings[step["save"]] = dict(
                status=reply.status,
                body=reply.body,
                raw=reply.raw.decode(),
                headers=reply.headers,
            )
        if "callback" in step:
            self.step(dict(op="callback", **step["callback"]))

    def step(self, step):
        op = step.get("op", "http")
        if op == "let":
            for name, value in step["values"].items():
                self.bindings[name] = (
                    self.suite.uid()
                    if value == {"uuid": True}
                    else expand(value, self.bindings)
                )
        elif op == "assert":
            for path, value in expand(step["equals"], self.bindings).items():
                self.suite.assertEqual(field(self.bindings, path), value)
        elif op == "parallel":

            def child(steps):
                instance = Scenario(self.suite, copy.deepcopy(self.bindings))
                instance.run({"steps": steps})
                return instance.bindings

            with ThreadPoolExecutor(max_workers=len(step["branches"])) as pool:
                values = list(pool.map(child, step["branches"]))
            self.bindings[step.get("save", "parallel")] = values
        elif op == "restart":
            self.suite.process.restart(hard=step.get("hard", False))
        elif op == "with_sql_fault":
            with self.fault(step["table"], step.get("sqlstate", "40001")):
                for child in step["steps"]:
                    self.step(child)
        elif op == "callback_fault":
            value = expand(step, self.bindings)
            self.suite.mock.fail_next(value["name"], value["id"], *value["statuses"])
        elif op == "callback":
            value = expand(step, self.bindings)
            events = self.suite.accepted(value["name"], value["id"])
            index = value.get("index", 0)
            if index >= len(events):
                from .wire import eventually

                events = eventually(
                    lambda: (
                        es
                        if len(
                            es := self.suite.mock.matching(value["name"], value["id"])
                        )
                        > index
                        else None
                    )
                )
            event = events[index]
            for path, expected in value.get("body", {}).items():
                self.suite.assertEqual(field(event["payload"], path), expected)
            if value.get("save"):
                self.bindings[value["save"]] = event
        elif op == "form":
            from .suite import HiddenForm

            target = expand(step["url"], self.bindings)
            u = urlsplit(target)
            if u.netloc and u.netloc != self.suite.client.base.netloc:
                raise ValueError("Forms must target the current local gateway")
            page = self.suite.client.request(
                "GET",
                u.path + ("?" + u.query if u.query else ""),
                signed=False,
                outside=True,
            )
            self.suite.assertEqual(page.status, 200)
            form = HiddenForm()
            form.feed(page.raw.decode())
            self.bindings[step["save"]] = {"action": form.action, "fields": form.fields}
        elif op == "form_submit":
            form = field(self.bindings, step["form"])
            values = dict(
                form["fields"], **expand(step.get("fields", {}), self.bindings)
            )
            reply = self.suite.client.request(
                "POST",
                form["action"],
                raw=urlencode(values).encode(),
                signed=False,
                outside=True,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            self.assertions(reply, step)
        elif op == "http":
            payload = (
                self.suite.fixture(step["fixture"])
                if "fixture" in step
                else expand(step.get("payload"), self.bindings)
            )
            for path, value in expand(step.get("set", {}), self.bindings).items():
                assign(payload, path, value)
            args = expand(step.get("request", {}), self.bindings)
            if step["method"] == "POST" and "idem" not in args:
                args["idem"] = self.suite.uid()
            reply = self.suite.client.request(
                step["method"], expand(step["path"], self.bindings), payload, **args
            )
            # Schema validation defaults to the declared request template.
            if "contract_path" not in step and not args.get("outside"):
                template = step["path"].split("?", 1)[0]
                step = dict(step, contract_path=template)
            self.assertions(reply, step)
        else:
            raise ValueError(f"Unknown scenario operation: {op}")


def load(path):
    value = json.loads(path.read_text())
    if isinstance(value, list):  # compatibility with the original six wire fixtures
        return value
    if value["version"] != 1:
        raise ValueError("Unsupported scenario bundle version")
    return value["scenarios"]
