// Package authz maps agentic-ops client certificates to Temporal claims, and limits worker
// certificates to the calls an activity worker needs.
//
// Open-source Temporal's default authorizer requires the same role (writer) to poll a task
// queue as to start, signal or cancel a workflow, so it cannot tell a worker from a starter.
// Worker certificates are handed to HTCondor jobs on shared machines; one that leaks must not
// be able to do more than serve its own task queues. They are signed by a worker-only
// intermediate CA, and the claim mapper accepts the worker role only from that intermediate
// and the other roles only from the root.
package authz

import (
	"bytes"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"regexp"
	"strings"

	"go.temporal.io/server/common/authorization"
	"google.golang.org/grpc/credentials"
)

// Certificate subjects: O=agentic-ops, OU=<role>, CN=<name>.
const (
	Organization = "agentic-ops"

	// RoleAdmin may do anything, including registering namespaces.
	RoleAdmin = "admin"
	// RoleController is a namespace writer: it runs workflows and starts, signals and
	// cancels them. Controller pods and the backend use it.
	RoleController = "controller"
	// RoleReader is a namespace reader: the Web UI and the read-only Temporal MCP server.
	RoleReader = "reader"
	// RoleWorker may only poll its own activity task queues and report on the tasks it got.
	RoleWorker = "worker"

	// AfIoName is the CN of the worker certificate for the shared af-io queue.
	AfIoName = "af-io"

	authTypeMTLS = "mtls"
)

// productionID is kept free of "-" so that the queue of one production can never match the
// pattern of another: ewkit-<production>-<memclass>.
var productionID = regexp.MustCompile(`^[a-z0-9]{1,40}$`)

var (
	errNoCertificate = errors.New("a client certificate is required")
	errNoChain       = errors.New("no verified certificate chain")
)

// issuer is which CA signed a client certificate. Worker certificates must come from a worker
// CA, the intermediate the ops-executor signs with; every other role must be signed by the root
// directly. A leaked worker CA key can then mint workers but never a controller or an admin.
type issuer int

const (
	issuerUnknown  issuer = iota
	issuerRoot            // directly by a CA in the frontend's clientCaFiles
	issuerWorkerCA        // by one of the configured worker CAs
)

// WorkerScope is what a worker certificate may touch. It travels in Claims.Extensions.
type WorkerScope struct {
	// Namespace is the only namespace the worker may call.
	Namespace string
	// Queue matches the task queues the worker may poll.
	Queue *regexp.Regexp
	// Production, when set, limits task tokens to workflows of that production:
	// production/<id> and dataset/<id>/... (workflow IDs are deterministic). Empty for af-io,
	// which serves every production.
	Production string
}

// AllowsWorkflow reports whether a task token of the given workflow may be used.
func (s *WorkerScope) AllowsWorkflow(workflowID string) bool {
	if s.Production == "" {
		return true
	}
	return workflowID == "production/"+s.Production ||
		strings.HasPrefix(workflowID, "dataset/"+s.Production+"/")
}

// ClaimMapper turns a client certificate into claims for one namespace, from its subject and
// the CA that signed it.
type ClaimMapper struct {
	namespace string
	workerCAs []*x509.Certificate
}

var _ authorization.ClaimMapper = (*ClaimMapper)(nil)

// NewClaimMapper scopes non-admin certificates to namespace. workerCAs are the intermediates
// that may sign worker certificates; with none, no worker certificate is accepted.
func NewClaimMapper(namespace string, workerCAs []*x509.Certificate) *ClaimMapper {
	return &ClaimMapper{namespace: namespace, workerCAs: workerCAs}
}

// LoadCertificates reads every certificate in a PEM file, e.g. the worker CAs: during a
// rotation the file holds the old and the new one.
func LoadCertificates(path string) ([]*x509.Certificate, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var certs []*x509.Certificate
	for block, rest := pem.Decode(data); block != nil; block, rest = pem.Decode(rest) {
		if block.Type != "CERTIFICATE" {
			continue
		}
		cert, err := x509.ParseCertificate(block.Bytes)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", path, err)
		}
		certs = append(certs, cert)
	}
	if len(certs) == 0 {
		return nil, fmt.Errorf("%s: no certificates", path)
	}
	return certs, nil
}

func (m *ClaimMapper) GetClaims(info *authorization.AuthInfo) (*authorization.Claims, error) {
	if info == nil || info.TLSSubject == nil {
		return nil, errNoCertificate
	}
	claims, err := m.claimsFor(info.TLSSubject, m.issuerOf(info.TLSConnection))
	if err != nil {
		// Temporal logs this as "Authorization error"; the subject says who it was
		return nil, fmt.Errorf("%s: %w", info.TLSSubject, err)
	}
	return claims, nil
}

// issuerOf reads the chains the TLS handshake verified. If any chain goes through a worker
// CA the certificate counts as a worker CA's, even if the worker CA was also (wrongly) made a
// trusted root.
func (m *ClaimMapper) issuerOf(conn *credentials.TLSInfo) issuer {
	if conn == nil {
		return issuerUnknown
	}
	found := issuerUnknown
	for _, chain := range conn.State.VerifiedChains {
		if len(chain) < 2 {
			continue
		}
		if m.isWorkerCA(chain[1]) {
			return issuerWorkerCA
		}
		if len(chain) == 2 {
			found = issuerRoot
		}
	}
	return found
}

func (m *ClaimMapper) isWorkerCA(cert *x509.Certificate) bool {
	for _, ca := range m.workerCAs {
		if bytes.Equal(ca.Raw, cert.Raw) {
			return true
		}
	}
	return false
}

func (m *ClaimMapper) claimsFor(subject *pkix.Name, signer issuer) (*authorization.Claims, error) {
	if len(subject.Organization) != 1 || subject.Organization[0] != Organization {
		return nil, fmt.Errorf("certificate organization is not %s", Organization)
	}
	if len(subject.OrganizationalUnit) != 1 {
		return nil, errors.New("certificate must have exactly one OU (its role)")
	}
	role := subject.OrganizationalUnit[0]
	switch {
	case signer == issuerUnknown:
		return nil, errNoChain
	case role == RoleWorker && signer != issuerWorkerCA:
		return nil, errors.New("worker certificates must be signed by a worker CA")
	case role != RoleWorker && signer != issuerRoot:
		return nil, fmt.Errorf("%s certificates must be signed by the root CA", role)
	}
	claims := &authorization.Claims{Subject: subject.String(), AuthType: authTypeMTLS}

	switch role {
	case RoleAdmin:
		claims.System = authorization.RoleAdmin
	case RoleController:
		claims.Namespaces = map[string]authorization.Role{m.namespace: authorization.RoleWriter}
	case RoleReader:
		claims.Namespaces = map[string]authorization.Role{m.namespace: authorization.RoleReader}
	case RoleWorker:
		scope, err := m.workerScope(subject.CommonName)
		if err != nil {
			return nil, err
		}
		claims.Namespaces = map[string]authorization.Role{m.namespace: authorization.RoleWorker}
		claims.Extensions = scope
	default:
		return nil, fmt.Errorf("unknown role %q", role)
	}
	return claims, nil
}

// workerScope reads the CN of a worker certificate: "af-io", or "ewkit-<production>" for the
// unit workers of one production.
func (m *ClaimMapper) workerScope(name string) (*WorkerScope, error) {
	if name == AfIoName {
		return &WorkerScope{
			Namespace: m.namespace,
			Queue:     regexp.MustCompile(`^` + regexp.QuoteMeta(AfIoName) + `$`),
		}, nil
	}
	if production, ok := strings.CutPrefix(name, "ewkit-"); ok && productionID.MatchString(production) {
		return &WorkerScope{
			Namespace:  m.namespace,
			Queue:      regexp.MustCompile(`^ewkit-` + production + `-[a-z0-9]+$`),
			Production: production,
		}, nil
	}
	return nil, fmt.Errorf("worker certificate CN %q is neither %s nor ewkit-<production>", name, AfIoName)
}
