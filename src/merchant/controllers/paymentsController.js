const Router = require('express').Router();
const u = require("../utils/responseTemplates");

// Initialize the payment; requires a proper order to be processed.
Router.get("/payments/init", (req, res) => {
    if(req.query.sessionId == null) {
        return res.status(403)
            .send(u.ErrorJSON("Unauthorized payment request."));
    }    

    let x = res.cache.get("session:" + req.query.sessionId);
    return res.render("payment_init", {
        data: x.data,
        res: x.res
    });
});

// Serves the success page.
Router.get("/payments/ok", async (_, res) => {
    let x = await res.uber.confirmDepositAsync(0);
    return res.render('payment_ok', {
        confirmRes: x.data
    });
});

// Confirms the deposit form the user side.
Router.post("/payments/confirm", (_, res) => {
    res.uber.confirmDepositAsync(0);
    return res.status(200).send();
});

// Cancels the deposit from the user side.
Router.post("/payments/cancel", (_, res) => {
    res.uber.cancelDepositAsync(0);
    return res.status(200).send();
});

module.exports = Router;