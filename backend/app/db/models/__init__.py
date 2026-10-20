"""ORM 模型汇总。"""
from app.db.models.base import Base
from app.db.models.chat import (Conversation, Message, QueryCache, QueryRecord, QueryStatus,
                                Report, ReportStatus, ReportTemplate, UserMemoryProfile)
from app.db.models.datasource import DataSource, DsStatus, DbType
from app.db.models.integration import (AirflowConfig, AzkabanConfig, CustomSkill, EtlTask,
                                       EtlTaskSource, EtlTaskStatus, EtlTaskVersion,
                                       IntegrationStatus, ModelingMessage, ModelingSession,
                                       ModelingSessionStatus, SchedulerRun, SchedulerRunStatus,
                                       SkillStatus, SkillType)
from app.db.models.governance import DwStandard, GovernanceRun, ProbeRun
from app.db.models.job import AuditLog, ScanJob, ScanJobStatus, ScanMode
from app.db.models.kg import (DimensionType, EdgeType, EntityType, KgDimension, KgEdge, KgMetric,
                              KgValidationRule, MetricStatus, RuleSeverity, RuleType)
from app.db.models.llm import (LlmCallLog, LlmConfig, LlmHealthStatus, LlmScene, LlmSceneBinding,
                               ModelKind, Protocol)
from app.db.models.metadata import (AggFunc, MetaColumn, MetaTable, SemanticRole, SourceKind,
                                    TableLayer)
from app.db.models.user import (DatasourcePermission, MaskStrategy, MaskingRule, Role, User,
                                UserStatus)

__all__ = [
    "Base",
    # user
    "User", "Role", "UserStatus", "DatasourcePermission", "MaskingRule", "MaskStrategy",
    # datasource
    "DataSource", "DbType", "DsStatus",
    # metadata
    "MetaTable", "MetaColumn", "TableLayer", "SemanticRole", "AggFunc", "SourceKind",
    # kg
    "KgMetric", "KgDimension", "KgEdge", "KgValidationRule",
    "MetricStatus", "DimensionType", "EdgeType", "EntityType", "RuleType", "RuleSeverity",
    # llm
    "LlmConfig", "LlmSceneBinding", "LlmCallLog", "Protocol", "ModelKind", "LlmScene", "LlmHealthStatus",
    # chat
    "Conversation", "Message", "QueryRecord", "QueryStatus", "QueryCache",
    "Report", "ReportStatus", "ReportTemplate", "UserMemoryProfile",
    # jobs
    "ScanJob", "ScanMode", "ScanJobStatus", "AuditLog",
    # integration / custom skills
    "AzkabanConfig", "AirflowConfig", "IntegrationStatus", "CustomSkill", "SkillType",
    "SkillStatus", "SchedulerRun", "SchedulerRunStatus",
    "EtlTask", "EtlTaskStatus", "EtlTaskSource", "EtlTaskVersion",
    "ModelingSession", "ModelingSessionStatus", "ModelingMessage",
    # governance / dw standards
    "DwStandard", "GovernanceRun", "ProbeRun",
]
