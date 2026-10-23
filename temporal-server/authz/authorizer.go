package authz

import (
	"context"
	"fmt"
	"strings"

	commonpb "go.temporal.io/api/common/v1"
	"go.temporal.io/api/workflowservice/v1"
	"go.temporal.io/server/common/api"
	"go.temporal.io/server/common/authorization"
	"go.temporal.io/server/common/log"
	"go.temporal.io/server/common/log/tag"
	"go.temporal.io/server/common/tasktoken"
)

// Workflow service methods a worker may call besides the health checks. Everything else,
// including PollWorkflowTaskQueue, the *ById variants, start, signal, cancel, terminate,
// update, query and list, is denied.
const (
	pollActivity       = "PollActivityTaskQueue"
	recordHeartbeat    = "RecordActivityTaskHeartbeat"
	respondCompleted   = "RespondActivityTaskCompleted"
	respondFailed      = "RespondActivityTaskFailed"
	respondCanceled    = "RespondActivityTaskCanceled"
	describeNamespace  = "DescribeNamespace"
	shutdownWorker     = "ShutdownWorker"
	recordWorkerStatus = "RecordWorkerHeartbeat"
)

// Authorizer applies WorkerScope to worker claims and Temporal's default authorizer to
// everything else (admin, controller and the internal frontend's system claims). Denied
// worker calls are logged, since a worker certificate trying something else is worth seeing.
type Authorizer struct {
	fallback authorization.Authorizer
	tokens   *tasktoken.Serializer
	logger   log.Logger
}

var _ authorization.Authorizer = (*Authorizer)(nil)

func NewAuthorizer(logger log.Logger) *Authorizer {
	return &Authorizer{fallback: authorization.NewDefaultAuthorizer(), tokens: tasktoken.NewSerializer(), logger: logger}
}

func (a *Authorizer) Authorize(ctx context.Context, claims *authorization.Claims, target *authorization.CallTarget) (authorization.Result, error) {
	scope, isWorker := workerScopeOf(claims)
	if !isWorker {
		return a.fallback.Authorize(ctx, claims, target)
	}
	if authorization.IsHealthCheckAPI(target.APIName) {
		return allow(claims), nil
	}
	if reason := a.checkWorker(scope, target); reason != "" {
		a.logger.Warn("Denied worker call",
			tag.NewStringTag("subject", claims.Subject),
			tag.NewStringTag("api", target.APIName),
			tag.WorkflowNamespace(target.Namespace),
			tag.NewStringTag("reason", reason))
		return authorization.Result{Decision: authorization.DecisionDeny, Reason: reason}, nil
	}
	return allow(claims), nil
}

func workerScopeOf(claims *authorization.Claims) (*WorkerScope, bool) {
	if claims == nil {
		return nil, false
	}
	scope, ok := claims.Extensions.(*WorkerScope)
	return scope, ok
}

// checkWorker returns why a worker call is denied, or "" to allow it.
func (a *Authorizer) checkWorker(scope *WorkerScope, target *authorization.CallTarget) string {
	method, ok := strings.CutPrefix(target.APIName, api.WorkflowServicePrefix)
	if !ok {
		return "workers may only call the workflow service"
	}
	if target.Namespace != scope.Namespace {
		return fmt.Sprintf("workers may only use namespace %s", scope.Namespace)
	}

	switch method {
	case describeNamespace, shutdownWorker, recordWorkerStatus:
		return ""
	case pollActivity:
		req, ok := target.Request.(*workflowservice.PollActivityTaskQueueRequest)
		if !ok {
			return "malformed poll request"
		}
		if queue := req.GetTaskQueue().GetName(); !scope.Queue.MatchString(queue) {
			return fmt.Sprintf("this worker may not poll task queue %q", queue)
		}
		return ""
	case recordHeartbeat, respondCompleted, respondFailed, respondCanceled:
		req, ok := target.Request.(interface{ GetTaskToken() []byte })
		if !ok {
			return "malformed activity task request"
		}
		token, err := a.tokens.Deserialize(req.GetTaskToken())
		if err != nil {
			return "malformed task token"
		}
		if !scope.AllowsWorkflow(token.GetWorkflowId()) {
			return "task token belongs to another production"
		}
		return ""
	default:
		return fmt.Sprintf("workers may not call %s", method)
	}
}

func allow(claims *authorization.Claims) authorization.Result {
	return authorization.Result{
		Decision:  authorization.DecisionAllow,
		Principal: &commonpb.Principal{Type: claims.AuthType, Name: claims.Subject},
	}
}
