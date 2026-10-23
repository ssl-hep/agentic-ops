# Phase 0 Temporal server: all services in one binary with the agentic-ops authorizer.
FROM golang:1.26 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -o /out/phase0-server ./cmd/phase0-server \
    && mkdir -p /out/data

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/phase0-server /usr/local/bin/phase0-server
COPY config/phase0.yaml /etc/temporal/config.yaml
COPY --from=build --chown=65532:65532 /out/data /var/lib/temporal
ENV TEMPORAL_DATA_DIR=/var/lib/temporal TEMPORAL_TLS_DIR=/etc/temporal/tls
VOLUME /var/lib/temporal
EXPOSE 7233 9090
ENTRYPOINT ["/usr/local/bin/phase0-server", "--config-file", "/etc/temporal/config.yaml"]
