"""orcho_mcp.schemas — Pydantic models for MCP tool results, split by family.

Convention: inputs use plain typed args (FastMCP infers schema from
type hints); outputs are Pydantic models so the wire contract is
explicit and round-trippable in tests.

Module layout:

  ``shared``       cross-family models (``NextActionRecord``).
  ``workspace``    orcho_workspace_info / orcho_workspace_state.
  ``read``         orcho_run_history / status / metrics / events_tail /
                   skills_list / profiles_list.
  ``observe``      orcho_run_events_summary / orcho_run_watch + the
                   handoff hint models that ride inside watch responses.
  ``authoring``    orcho_plan_validate / orcho_prompts_resolve.
  ``run_control``  orcho_run_start / resume / cancel / phase_handoff_decide.
  ``inspection``   orcho_run_evidence / orcho_run_diff.

This ``__init__`` re-exports every model from the family modules so
existing callers (``from orcho_mcp.schemas import RunStatus``,
etc.) keep working without churn. Domain modules can also be imported
directly when a caller wants the narrower surface
(``from orcho_mcp.schemas.read import RunStatus``).
"""
from __future__ import annotations

from orcho_mcp.schemas.authoring import (
    PlanValidateResult,
    PromptChainEntry,
    PromptResolveResult,
    SubTaskRecord,
)
from orcho_mcp.schemas.inspection import (
    CriterionReportRecord,
    DeliveryActionRecord,
    DeliveryGateDiffSummary,
    DeliveryGateProjection,
    ErrorsHaltSliceRecord,
    EvidenceArtifactSliceRecord,
    EvidenceCommandSliceRecord,
    EvidenceResult,
    FindingRecord,
    HandoffAdviceCallRecord,
    HandoffAdviceSliceRecord,
    HandoffAdviceSummaryRecord,
    HandoffAdviceUsageRecord,
    ImplementDeliveryRecord,
    PlanSliceRecord,
    RunDiffFile,
    RunDiffResult,
    SubRunLinkRecord,
    SubtaskReceiptRecord,
    VerificationAutorunEventRecord,
    VerificationCheckRecord,
    VerificationCockpit,
    VerificationCommandRecord,
    VerificationGateCockpitRow,
    VerificationReceiptRecord,
    VerificationTimelineGateRecord,
    VerificationTimelineRecord,
)
from orcho_mcp.schemas.observe import (
    CompactRunEvent,
    CurrentSubtaskRecord,
    HandoffClientHints,
    HandoffDecisionChoice,
    HandoffDecisionHint,
    HandoffElicitationHint,
    HandoffFindingSummary,
    HandoffFollowupCall,
    PendingHandoffSummary,
    PhaseEventSummary,
    ProviderSessionFallback,
    RetryState,
    RunEventsSummary,
    RunLiveActivity,
    RunLiveHandoff,
    RunLiveStatusCard,
    RunLiveTerminal,
    RunWatchResult,
    WatchTrigger,
)
from orcho_mcp.schemas.read import (
    ArtefactRefRecord,
    AutoDetectProjection,
    EventRecord,
    EventsTailResult,
    FollowupLineage,
    HistoryResult,
    ProfileHypothesisRecord,
    ProfileRecord,
    ProfileSelectorRecord,
    ProfilesListResult,
    RecoveryRecommendation,
    RunMetrics,
    RunRecord,
    RunStatus,
    SkillRecord,
    SkillsListResult,
    WorktreeContinuity,
)
from orcho_mcp.schemas.run_control import (
    CancelResult,
    DeliveryDecideResult,
    HandoffAdviceResult,
    HandoffAdviceSafetyRecord,
    InspectOnlyControlResult,
    PhaseHandoffDecideResult,
    ResumeBlockedResult,
    ResumePendingDecisionResult,
    RunDiagnosis,
    RunResumeResult,
    RunStartedResult,
    RuntimeOverrideArg,
    TypedRunResult,
    TypedRunStartedResult,
)
from orcho_mcp.schemas.shared import (
    ContinuationSubjectLiteral,
    NextActionRecord,
    ProviderPressure,
    RecommendedNextActionLiteral,
    RecoveryLineage,
)
from orcho_mcp.schemas.workflows import (
    RecipeBranchStep,
    RecipeInput,
    RecipeStep,
    RecipeToolStep,
    WorkflowRecipe,
    WorkflowRecipeList,
)
from orcho_mcp.schemas.workspace import (
    WorkspaceInfo,
    WorkspaceMcpStateResult,
    WorkspacePendingDecisionRow,
    WorkspacePendingDecisionsResult,
    WorkspaceRunStateRecord,
)

__all__ = [
    # shared
    "ContinuationSubjectLiteral",
    "NextActionRecord",
    "ProviderPressure",
    "RecommendedNextActionLiteral",
    "RecoveryLineage",
    # workspace
    "WorkspaceInfo",
    "WorkspaceMcpStateResult",
    "WorkspacePendingDecisionRow",
    "WorkspacePendingDecisionsResult",
    "WorkspaceRunStateRecord",
    # read
    "ArtefactRefRecord",
    "AutoDetectProjection",
    "EventRecord",
    "EventsTailResult",
    "FollowupLineage",
    "HistoryResult",
    "ProfileHypothesisRecord",
    "ProfileRecord",
    "ProfileSelectorRecord",
    "ProfilesListResult",
    "RecoveryRecommendation",
    "RunMetrics",
    "RunRecord",
    "RunStatus",
    "SkillRecord",
    "SkillsListResult",
    "WorktreeContinuity",
    # observe
    "CompactRunEvent",
    "CurrentSubtaskRecord",
    "HandoffClientHints",
    "HandoffDecisionChoice",
    "HandoffDecisionHint",
    "HandoffElicitationHint",
    "HandoffFindingSummary",
    "HandoffFollowupCall",
    "PendingHandoffSummary",
    "PhaseEventSummary",
    "ProviderSessionFallback",
    "RetryState",
    "RunEventsSummary",
    "RunLiveActivity",
    "RunLiveHandoff",
    "RunLiveStatusCard",
    "RunLiveTerminal",
    "RunWatchResult",
    "WatchTrigger",
    # authoring
    "PlanValidateResult",
    "PromptChainEntry",
    "PromptResolveResult",
    "SubTaskRecord",
    # run_control
    "CancelResult",
    "DeliveryDecideResult",
    "HandoffAdviceResult",
    "HandoffAdviceSafetyRecord",
    "InspectOnlyControlResult",
    "PhaseHandoffDecideResult",
    "ResumeBlockedResult",
    "ResumePendingDecisionResult",
    "RunDiagnosis",
    "RunResumeResult",
    "RunStartedResult",
    "RuntimeOverrideArg",
    "TypedRunResult",
    "TypedRunStartedResult",
    # inspection
    "CriterionReportRecord",
    "DeliveryActionRecord",
    "DeliveryGateDiffSummary",
    "DeliveryGateProjection",
    "ErrorsHaltSliceRecord",
    "EvidenceArtifactSliceRecord",
    "EvidenceCommandSliceRecord",
    "EvidenceResult",
    "FindingRecord",
    "HandoffAdviceCallRecord",
    "HandoffAdviceSliceRecord",
    "HandoffAdviceSummaryRecord",
    "HandoffAdviceUsageRecord",
    "ImplementDeliveryRecord",
    "PlanSliceRecord",
    "RunDiffFile",
    "RunDiffResult",
    "SubRunLinkRecord",
    "SubtaskReceiptRecord",
    "VerificationAutorunEventRecord",
    "VerificationCheckRecord",
    "VerificationCockpit",
    "VerificationCommandRecord",
    "VerificationGateCockpitRow",
    "VerificationReceiptRecord",
    "VerificationTimelineGateRecord",
    "VerificationTimelineRecord",
    # workflows
    "RecipeBranchStep",
    "RecipeInput",
    "RecipeStep",
    "RecipeToolStep",
    "WorkflowRecipe",
    "WorkflowRecipeList",
]
