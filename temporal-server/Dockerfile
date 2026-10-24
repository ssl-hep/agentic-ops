# Temporal server with the agentic-ops authorizer.
#
#   docker build temporal-server                    production server on Postgres (default)
#   docker build --target phase0 temporal-server    phase 0 test server on SQLite
FROM golang:1.26 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
# temporal-sql-tool comes from the same module version as the server, so its embedded
# schemas always match what the server expects.
RUN export CGO_ENABLED=0 \
    && go build -trimpath -tags disable_grpc_modules -o /out/temporal-server ./cmd/temporal-server \
    && go build -trimpath -tags disable_grpc_modules -o /out/temporal-sql-tool go.temporal.io/server/cmd/tools/sql \
    && go build -trimpath -tags disable_grpc_modules -o /out/phase0-server ./cmd/phase0-server \
    && mkdir -p /out/data

FROM gcr.io/distroless/static-debian12:nonroot AS phase0
COPY --from=build /out/phase0-server /usr/local/bin/phase0-server
COPY config/phase0.yaml /etc/temporal/config.yaml
COPY --from=build --chown=65532:65532 /out/data /var/lib/temporal
ENV TEMPORAL_DATA_DIR=/var/lib/temporal TEMPORAL_TLS_DIR=/etc/temporal/tls
VOLUME /var/lib/temporal
EXPOSE 7233 9090
ENTRYPOINT ["/usr/local/bin/phase0-server", "--config-file", "/etc/temporal/config.yaml"]

FROM gcr.io/distroless/static-debian12:nonroot AS production
COPY --from=build /out/temporal-server /out/temporal-sql-tool /usr/local/bin/
COPY config/production.yaml /etc/temporal/config.yaml
ENV TEMPORAL_TLS_DIR=/etc/temporal/tls
EXPOSE 7233 9090
ENTRYPOINT ["/usr/local/bin/temporal-server", "--config-file", "/etc/temporal/config.yaml"]
