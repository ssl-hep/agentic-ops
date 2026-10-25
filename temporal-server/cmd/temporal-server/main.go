// Command temporal-server runs every Temporal service in one process with the agentic-ops
// claim mapper and authorizer, on Postgres. It is the production server deployed by
// charts/temporal: frontend mTLS, and an internal frontend for Temporal's own system worker.
package main

import (
	"flag"
	stdlog "log"
	_ "time/tzdata" // embed tzdata as a fallback

	"github.com/ssl-hep/agentic-ops/temporal-server/authz"
	"go.temporal.io/server/common/authorization"
	"go.temporal.io/server/common/config"
	"go.temporal.io/server/common/log"
	_ "go.temporal.io/server/common/persistence/sql/sqlplugin/postgresql" // Postgres persistence
	"go.temporal.io/server/temporal"
)

func main() {
	configFile := flag.String("config-file", "/etc/temporal/config.yaml", "server config file")
	namespace := flag.String("namespace", "ewkit-production", "the namespace that controller, reader and worker certificates are scoped to")
	workerCAFile := flag.String("worker-ca", "/etc/temporal/tls/worker-ca.pem", "the intermediate CAs that may sign worker certificates (PEM)")
	flag.Parse()

	workerCAs, err := authz.LoadCertificates(*workerCAFile)
	if err != nil {
		stdlog.Fatalf("loading worker CAs: %v", err)
	}

	cfg, err := config.Load(config.WithConfigFile(*configFile))
	if err != nil {
		stdlog.Fatalf("loading %s: %v", *configFile, err)
	}
	logger := log.NewZapLogger(log.BuildZapLogger(cfg.Log))

	s, err := temporal.NewServer(
		temporal.ForServices(temporal.Services),
		temporal.WithConfig(cfg),
		temporal.WithLogger(logger),
		temporal.InterruptOn(temporal.InterruptCh()),
		temporal.WithAuthorizer(authz.NewAuthorizer(logger)),
		temporal.WithClaimMapper(func(*config.Config) authorization.ClaimMapper {
			return authz.NewClaimMapper(*namespace, workerCAs)
		}),
	)
	if err != nil {
		stdlog.Fatalf("creating server: %v", err)
	}
	if err := s.Start(); err != nil {
		stdlog.Fatalf("starting server: %v", err)
	}
}
