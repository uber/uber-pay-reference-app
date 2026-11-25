FROM golang:1.25-alpine@sha256:1ae0735f00daffa3aaf1363a5184c0d2dc55c78e3db4ec70241cdac97bf84b59 AS build
WORKDIR /src
COPY implementations/go/go.mod implementations/go/go.sum ./
RUN go mod download
COPY implementations/go/ ./
RUN CGO_ENABLED=0 go build -trimpath -o /psp ./cmd/psp

FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
WORKDIR /app
COPY --from=build /psp /app/psp
COPY contracts/ /app/contracts/
COPY storage/migrations/ /app/storage/migrations/
ENV HOST=0.0.0.0 PORT=8080
EXPOSE 8080
ENTRYPOINT ["/app/psp"]
