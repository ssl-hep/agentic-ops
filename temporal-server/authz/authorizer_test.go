package authz

import (
	"context"
	"testing"

	taskqueuepb "go.temporal.io/api/taskqueue/v1"
	"go.temporal.io/api/workflowservice/v1"
	tokenspb "go.temporal.io/server/api/token/v1"
	"go.temporal.io/server/common/api"
	"go.temporal.io/server/common/authorization"
	"go.temporal.io/server/common/log"
)

const ns = "phase0"

func token(t *testing.T, workflowID string) []byte {
	t.Helper()
	b, err := (&tokenspb.Task{WorkflowId: workflowID, ActivityId: "1"}).Marshal()
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func poll(queue string) any {
	return &workflowservice.PollActivityTaskQueueRequest{Namespace: ns, TaskQueue: &taskqueuepb.TaskQueue{Name: queue}}
}

func TestAuthorize(t *testing.T) {
	p := newTestPKI(t)
	admin := p.claims(t, RoleAdmin, "ben")
	controller := p.claims(t, RoleController, "controller")
	reader := p.claims(t, RoleReader, "web-ui")
	unitWorker := p.claims(t, RoleWorker, "ewkit-phase0")
	afIo := p.claims(t, RoleWorker, AfIoName)

	wf := func(method string) string { return api.WorkflowServicePrefix + method }
	op := func(method string) string { return api.OperatorServicePrefix + method }

	cases := []struct {
		name    string
		claims  *authorization.Claims
		api     string
		ns      string
		request any
		allowed bool
	}{
		{"anonymous health check", nil, wf("GetSystemInfo"), "", nil, true},
		{"anonymous start", nil, wf("StartWorkflowExecution"), ns, nil, false},

		{"admin registers namespace", admin, wf("RegisterNamespace"), "", nil, true},
		{"admin adds search attributes", admin, op("AddSearchAttributes"), ns, nil, true},
		{"controller starts workflow", controller, wf("StartWorkflowExecution"), ns, nil, true},
		{"controller polls workflow tasks", controller, wf("PollWorkflowTaskQueue"), ns, nil, true},
		{"controller signals", controller, wf("SignalWorkflowExecution"), ns, nil, true},
		{"controller in other namespace", controller, wf("StartWorkflowExecution"), "default", nil, false},
		{"controller registers namespace", controller, wf("RegisterNamespace"), "", nil, false},

		{"reader lists workflows", reader, wf("ListWorkflowExecutions"), ns, nil, true},
		{"reader describes workflow", reader, wf("DescribeWorkflowExecution"), ns, nil, true},
		{"reader reads history", reader, wf("GetWorkflowExecutionHistory"), ns, nil, true},
		{"reader describes namespace", reader, wf("DescribeNamespace"), ns, nil, true},
		{"reader starts workflow", reader, wf("StartWorkflowExecution"), ns, nil, false},
		{"reader signals", reader, wf("SignalWorkflowExecution"), ns, nil, false},
		{"reader terminates", reader, wf("TerminateWorkflowExecution"), ns, nil, false},
		{"reader polls activity tasks", reader, wf("PollActivityTaskQueue"), ns, poll("ewkit-phase0-8g"), false},
		{"reader in other namespace", reader, wf("ListWorkflowExecutions"), "default", nil, false},
		{"reader adds search attributes", reader, op("AddSearchAttributes"), ns, nil, false},

		{"worker system info", unitWorker, wf("GetSystemInfo"), "", nil, true},
		{"worker describes namespace", unitWorker, wf("DescribeNamespace"), ns, nil, true},
		{"worker polls own queue", unitWorker, wf("PollActivityTaskQueue"), ns, poll("ewkit-phase0-8g"), true},
		{"worker polls other production", unitWorker, wf("PollActivityTaskQueue"), ns, poll("ewkit-phase1-8g"), false},
		{"worker polls production with shared prefix", unitWorker, wf("PollActivityTaskQueue"), ns, poll("ewkit-phase0x-8g"), false},
		{"worker polls af-io", unitWorker, wf("PollActivityTaskQueue"), ns, poll("af-io"), false},
		{"worker polls in other namespace", unitWorker, wf("PollActivityTaskQueue"), "default", poll("ewkit-phase0-8g"), false},
		{"worker heartbeats own dataset", unitWorker, wf("RecordActivityTaskHeartbeat"), ns,
			&workflowservice.RecordActivityTaskHeartbeatRequest{Namespace: ns, TaskToken: token(t, "dataset/phase0/mc23_13p6TeV.1")}, true},
		{"worker completes smoke test", unitWorker, wf("RespondActivityTaskCompleted"), ns,
			&workflowservice.RespondActivityTaskCompletedRequest{Namespace: ns, TaskToken: token(t, "production/phase0")}, true},
		{"worker completes other production", unitWorker, wf("RespondActivityTaskCompleted"), ns,
			&workflowservice.RespondActivityTaskCompletedRequest{Namespace: ns, TaskToken: token(t, "dataset/phase1/x")}, false},
		{"worker fails other production", unitWorker, wf("RespondActivityTaskFailed"), ns,
			&workflowservice.RespondActivityTaskFailedRequest{Namespace: ns, TaskToken: token(t, "production/phase01")}, false},
		{"worker sends garbage token", unitWorker, wf("RespondActivityTaskCanceled"), ns,
			&workflowservice.RespondActivityTaskCanceledRequest{Namespace: ns, TaskToken: []byte{0xff, 0xff, 0xff}}, false},
		{"worker shuts down", unitWorker, wf("ShutdownWorker"), ns, &workflowservice.ShutdownWorkerRequest{Namespace: ns}, true},
		{"worker starts workflow", unitWorker, wf("StartWorkflowExecution"), ns, nil, false},
		{"worker signals", unitWorker, wf("SignalWorkflowExecution"), ns, nil, false},
		{"worker cancels", unitWorker, wf("RequestCancelWorkflowExecution"), ns, nil, false},
		{"worker terminates", unitWorker, wf("TerminateWorkflowExecution"), ns, nil, false},
		{"worker updates", unitWorker, wf("UpdateWorkflowExecution"), ns, nil, false},
		{"worker lists", unitWorker, wf("ListWorkflowExecutions"), ns, nil, false},
		{"worker reads history", unitWorker, wf("GetWorkflowExecutionHistory"), ns, nil, false},
		{"worker polls workflow tasks", unitWorker, wf("PollWorkflowTaskQueue"), ns, nil, false},
		{"worker completes by id", unitWorker, wf("RespondActivityTaskCompletedById"), ns, nil, false},
		{"worker calls operator service", unitWorker, op("ListSearchAttributes"), ns, nil, false},

		{"af-io polls af-io", afIo, wf("PollActivityTaskQueue"), ns, poll("af-io"), true},
		{"af-io polls unit queue", afIo, wf("PollActivityTaskQueue"), ns, poll("ewkit-phase0-8g"), false},
		{"af-io completes any production", afIo, wf("RespondActivityTaskCompleted"), ns,
			&workflowservice.RespondActivityTaskCompletedRequest{Namespace: ns, TaskToken: token(t, "dataset/phase1/x")}, true},
		{"af-io starts workflow", afIo, wf("StartWorkflowExecution"), ns, nil, false},
	}

	a := NewAuthorizer(log.NewNoopLogger())
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			result, err := a.Authorize(context.Background(), c.claims, &authorization.CallTarget{APIName: c.api, Namespace: c.ns, Request: c.request})
			if err != nil {
				t.Fatal(err)
			}
			if got := result.Decision == authorization.DecisionAllow; got != c.allowed {
				t.Errorf("allowed = %v, want %v (reason %q)", got, c.allowed, result.Reason)
			}
		})
	}
}
