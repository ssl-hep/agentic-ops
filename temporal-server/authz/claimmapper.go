// Package authz maps agentic-ops client certificates to Temporal claims, and limits worker
// certificates to the calls an activity worker needs.
//
// Open-source Temporal's default authorizer requires the same role (writer) to poll a task
// queue as to start, signal or cancel a workflow, so it cannot tell a worker from a starter.
// Worker certificates are handed to HTCondor jobs on shared machines; one that leaks must not
// be able to do more than serve its own task queues.
package authz

import (
	"crypto/x509/pkix"
	"errors"
	"fmt"
	"regexp"
	"strings"

	"go.temporal.io/server/common/authorization"
)

// Certificate subjects: O=agentic-ops, OU=<role>, CN=<name>.
const (
	Organization = "agentic-ops"

	// RoleAdmin may do anything, including registering namespaces.
	RoleAdmin = "admin"
	// RoleController is a namespace writer: it runs workflows and starts, signals and
	// cancels them. Controller pods and the backend use it.
	RoleController = "controller"
	// RoleWorker may only poll its own activity task queues and report on the tasks it got.
	RoleWorker = "worker"

	// AfIoName is the CN of the worker certificate for the shared af-io queue.
	AfIoName = "af-io"

	authTypeMTLS = "mtls"
)

// productionID is kept free of "-" so that the queue of one production can never match the
// pattern of another: ewkit-<production>-<memclass>.
var productionID = regexp.MustCompile(`^[a-z0-9]{1,40}$`)

var errNoCertificate = errors.New("a client certificate is required")

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

// ClaimMapper turns a client certificate subject into claims for one namespace.
type ClaimMapper struct {
	namespace string
}

var _ authorization.ClaimMapper = (*ClaimMapper)(nil)

func NewClaimMapper(namespace string) *ClaimMapper {
	return &ClaimMapper{namespace: namespace}
}

func (m *ClaimMapper) GetClaims(info *authorization.AuthInfo) (*authorization.Claims, error) {
	if info == nil || info.TLSSubject == nil {
		return nil, errNoCertificate
	}
	return m.claimsFor(info.TLSSubject)
}

func (m *ClaimMapper) claimsFor(subject *pkix.Name) (*authorization.Claims, error) {
	if len(subject.Organization) != 1 || subject.Organization[0] != Organization {
		return nil, fmt.Errorf("certificate organization is not %s", Organization)
	}
	if len(subject.OrganizationalUnit) != 1 {
		return nil, errors.New("certificate must have exactly one OU (its role)")
	}
	claims := &authorization.Claims{Subject: subject.String(), AuthType: authTypeMTLS}

	switch role := subject.OrganizationalUnit[0]; role {
	case RoleAdmin:
		claims.System = authorization.RoleAdmin
	case RoleController:
		claims.Namespaces = map[string]authorization.Role{m.namespace: authorization.RoleWriter}
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
