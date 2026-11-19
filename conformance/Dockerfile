FROM python:3.13-slim
WORKDIR /workspace
COPY conformance/requirements.txt conformance/requirements.txt
RUN pip install --no-cache-dir --index-url https://pypi.org/simple -r conformance/requirements.txt
COPY contracts/ contracts/
COPY conformance/ conformance/
COPY tools/ tools/
EXPOSE 9090
CMD ["python", "-m", "conformance.mock_uber", "--port", "9090"]
