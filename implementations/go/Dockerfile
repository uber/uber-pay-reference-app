FROM golang:1.25-alpine AS build
WORKDIR /src
COPY implementations/go/go.mod implementations/go/go.sum ./
RUN go mod download
COPY implementations/go/ ./
RUN CGO_ENABLED=0 go build -trimpath -o /psp ./cmd/psp

FROM gcr.io/distroless/static-debian12:nonroot
WORKDIR /app
COPY --from=build /psp /app/psp
COPY contracts/ /app/contracts/
COPY storage/migrations/ /app/storage/migrations/
ENV HOST=0.0.0.0 PORT=8080
EXPOSE 8080
ENTRYPOINT ["/app/psp"]
