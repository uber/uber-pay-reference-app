FROM python:3.13-slim@sha256:6d68eb6310a906f26c7638eb6ea7557d4261662df3181af2f80f1f9a7e34c549
WORKDIR /workspace
COPY conformance/requirements.txt conformance/requirements.txt
RUN pip install --no-cache-dir --index-url https://pypi.org/simple -r conformance/requirements.txt
COPY contracts/ contracts/
COPY conformance/ conformance/
COPY tools/ tools/
RUN mkdir -p /keys /public-keys && chown -R 10001:10001 /keys /public-keys /workspace
USER 10001:10001
EXPOSE 9090
CMD ["python", "-m", "conformance.mock_uber", "--port", "9090"]
