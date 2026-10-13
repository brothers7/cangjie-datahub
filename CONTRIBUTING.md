# Contributing to Cangjie DataHub / 参与贡献

**English** | [中文](#中文)

First off, thank you for considering a contribution! 🎉

## Ways to contribute

- 🐛 **Bug reports** — open an issue with reproduction steps, expected vs actual behavior, and your environment (OS, DB type, versions).
- 💡 **Feature requests** — open an issue describing the use case first; big features are best discussed before coding.
- 🔧 **Pull requests** — bug fixes, new connectors, scheduler skills, docs, translations.
- 🌐 **Translations** — the UI is bilingual (en/zh); new languages live in `frontend/src/i18n/dict/`.

## Development setup

```bash
# Backend (Python 3.11+)
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --port 9111 --reload

# Frontend (Node 18+)
cd frontend && npm install
VITE_DEV_API=http://127.0.0.1:9111 npm run dev
```

## Before submitting a PR

```bash
cd backend && python -m pytest tests/          # all backend tests must pass
cd frontend && npx tsc --noEmit && npm run build
```

- Add tests for new backend behavior (`backend/tests/`, isolated SQLite fixtures — see `tests/conftest.py`).
- New UI strings must go through the i18n dictionaries (`frontend/src/i18n/dict/*.ts`, both `en` and `zh`).
- Keep PRs focused; one concern per PR.

## Security perimeter — please keep it intact

This project treats safety as architecture, not prompting. When touching these areas, do not weaken them:

- SQL execution is **SELECT-only** (sqlglot AST guard, table whitelist, forced LIMIT)
- Credentials are **AES-256-GCM encrypted** and never returned to the frontend
- **DDL is never auto-executed**; scheduled writes require dual human gates (task approval + datasource opt-in)
- Custom skills run **sandboxed** (no network, no disk writes, static blacklist scan on upload)
- New endpoints must enforce authentication and role/permission checks, and write audit logs for sensitive actions

If you discover a security issue, please **do not open a public issue** — contact us directly (QQ 763342138 / 763342138@qq.com).

## Code style

- Backend: PEP 8, type hints, Chinese comments are fine and common in this codebase
- Frontend: existing patterns (function components, zustand stores, CSS variables for theming)
- Commit messages: conventional style appreciated (`feat:`, `fix:`, `docs:`…)

---

## 中文

感谢你的贡献！🎉

### 贡献方式

- 🐛 **报 Bug**——提 issue 时请附复现步骤、期望与实际表现、环境信息（系统、数据库类型、版本）
- 💡 **提需求**——先开 issue 描述使用场景，大功能建议先讨论再动手
- 🔧 **提 PR**——修 bug、新连接器、调度 Skill、文档、翻译都欢迎
- 🌐 **翻译**——界面双语（英/中），词条在 `frontend/src/i18n/dict/`

### 开发环境

```bash
# 后端（Python 3.11+）
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --port 9111 --reload

# 前端（Node 18+）
cd frontend && npm install
VITE_DEV_API=http://127.0.0.1:9111 npm run dev
```

### 提交 PR 前

```bash
cd backend && python -m pytest tests/          # 后端测试必须全绿
cd frontend && npx tsc --noEmit && npm run build
```

- 后端新行为请补测试（`backend/tests/`，隔离 SQLite 夹具，参考 `tests/conftest.py`）
- 新界面文案必须进 i18n 词典（`frontend/src/i18n/dict/*.ts`，`en`/`zh` 双语）
- 一个 PR 只解决一件事

### 安全边界——请勿削弱

本项目把安全做进架构而非提示词。改动以下区域时请保持边界完整：

- SQL 执行**仅 SELECT**（sqlglot AST 守卫、表白名单、强制 LIMIT）
- 凭证 **AES-256-GCM 加密**，前端永不回显
- **DDL 绝不自动执行**；调度写入需双重人工闸门（任务审核 + 数据源开启）
- 自定义 Skill **沙箱运行**（禁网、禁写盘、上传静态黑名单扫描）
- 新接口必须鉴权并做角色/权限校验，敏感操作写审计日志

发现安全问题请**不要开公开 issue**，直接联系我们（QQ 763342138 / 763342138@qq.com）。

### 代码风格

- 后端：PEP 8 + 类型标注；代码注释用中文即可
- 前端：遵循现有模式（函数组件、zustand、CSS 变量主题）
- 提交信息建议约定式（`feat:` / `fix:` / `docs:` …）
