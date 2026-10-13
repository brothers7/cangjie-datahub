"""PRD 3.3A/3.3B/3.3C：库表答疑、ETL 助手（含 Azkaban 工程）、自定义 Skill。"""
from __future__ import annotations

import io
import zipfile

import pytest

from app.agent import router_agent
from app.agent.skills import etl, table_qa
from app.db.models import (CustomSkill, DataSource, DbType, KgMetric, MetaColumn, MetaTable,
                           MetricStatus, SkillStatus, SkillType, SourceKind, TableLayer)
from app.services import azkaban as azk
from app.services import custom_skills as cs


def _ds():
    import uuid
    return DataSource(name=f"测试仓-{uuid.uuid4().hex[:8]}", db_type=DbType.MYSQL,
                      host="127.0.0.1", port=3306, database_name="demo", username="r")


async def _seed_tables(session, ds):
    t1 = MetaTable(datasource_id=ds.id, table_name="dws_trade_daily",
                   comment="交易按天汇总表", layer=TableLayer.DWS,
                   partition_cols=["dt"], row_count=100000)
    t2 = MetaTable(datasource_id=ds.id, table_name="dim_channel",
                   comment="渠道维表", layer=TableLayer.DIM)
    session.add_all([t1, t2])
    await session.flush()
    session.add_all([
        MetaColumn(table_id=t1.id, column_name="dt", data_type="string",
                   comment="日期分区", semantic_role="time"),
        MetaColumn(table_id=t1.id, column_name="gmv", data_type="double",
                   comment="含税GMV", semantic_role="metric", agg_hint="sum"),
        MetaColumn(table_id=t1.id, column_name="channel_id", data_type="string",
                   comment="渠道ID", semantic_role="dimension"),
        MetaColumn(table_id=t2.id, column_name="channel_id", data_type="string",
                   comment="渠道ID", semantic_role="dimension"),
    ])
    await session.commit()
    return t1, t2


# ================= 路由 =================

class TestRouting:
    def test_table_qa_rules(self):
        for q in ["dws_trade_daily 这张表是干嘛的", "我应该用哪张表算渠道转化率",
                  "gmv字段是什么含义", "订单表怎么关联用户表", "这张表的使用建议"]:
            intent, conf = router_agent.route_by_rules(q)
            assert intent == "table_qa", (q, intent)
            assert conf >= 0.8

    def test_etl_rules(self):
        for q in ["帮我新建一张按天汇总GMV的表", "开发一个渠道日报表", "ETL 加工需求：订单宽表"]:
            intent, _ = router_agent.route_by_rules(q)
            assert intent == "etl_design", (q, intent)

    def test_metric_qa_rules(self):
        """PRD 3.3-7：指标口径 / 指标查表引导 路由到 table_qa。"""
        for q in ["GMV 的口径是什么", "客单价怎么计算", "支付成功率的分子分母是什么",
                  "GMV 要到哪张表里查", "转化率在哪个表查", "UV 的定义"]:
            intent, conf = router_agent.route_by_rules(q)
            assert intent == "table_qa", (q, intent)
            assert conf >= 0.8

    def test_query_rules_not_hijacked(self):
        # 正常查数问题不应被新意图抢走
        for q, want in [("最近30天GMV趋势", "trend"), ("昨日各渠道销售额排行", "ranking")]:
            intent, _ = router_agent.route_by_rules(q)
            assert intent == want, (q, intent)

    async def test_custom_skill_keyword_priority(self, session):
        s = CustomSkill(name="weekly", description="周报", version=1,
                        skill_type=SkillType.PROMPT, trigger_keywords=["周报"],
                        status=SkillStatus.ENABLED, files={"prompt.md": "x"}, entry="prompt.md")
        intent, conf = await router_agent.route_intent("帮我生成本周周报", custom_skills=[s])
        assert intent == "custom_skill:weekly"
        assert conf >= 0.9
        # 停用/未命中 → 不拦截
        intent2, _ = await router_agent.route_intent("最近30天GMV趋势", custom_skills=[s])
        assert intent2 == "trend"


# ================= 库表答疑 =================

class TestTableQa:
    async def test_context_with_matched_table(self, session):
        ds = _ds()
        session.add(ds)
        await session.flush()
        t1, _ = await _seed_tables(session, ds)

        ctx, matched, _ = await table_qa.build_context(session, ds.id, "dws_trade_daily 是干嘛的")
        assert matched == ["dws_trade_daily"]
        assert "交易按天汇总表" in ctx
        assert "gmv" in ctx and "含税GMV" in ctx
        assert "分区字段: dt" in ctx

    async def test_context_overview_when_no_match(self, session):
        ds = _ds()
        session.add(ds)
        await session.flush()
        await _seed_tables(session, ds)

        ctx, matched, _ = await table_qa.build_context(session, ds.id, "有哪些表可以用")
        assert matched == []
        assert "dws_trade_daily" in ctx and "dim_channel" in ctx  # 全量清单

    async def test_empty_datasource(self, session):
        ds = _ds()
        session.add(ds)
        await session.commit()
        ctx, matched, _ = await table_qa.build_context(session, ds.id, "任意问题")
        assert ctx == "" and matched == []

    async def test_answer_degrades_without_llm(self, session):
        """无可用 LLM（未配置）时降级为目录摘要而不是报错。"""
        ds = _ds()
        session.add(ds)
        await session.flush()
        await _seed_tables(session, ds)
        chunks = []
        async for delta, *_ in table_qa.table_qa_answer(
                session=session, question="dws_trade_daily 是干嘛的", datasource_id=ds.id):
            chunks.append(delta)
        text = "".join(chunks)
        assert text  # 有输出
        assert "dws_trade_daily" in text


# ================= ETL 助手 =================

class TestEtlValidate:
    def test_naming_and_duplicate(self):
        d = etl.EtlDesign(target_table="TradeDaily", etl_sql="INSERT OVERWRITE TABLE t SELECT SUM(x) FROM d GROUP BY dt")
        etl.validate_design(d, {"tradedaily", "dws_trade_daily"}, "hive", "按天汇总")
        assert any("命名规范" in w for w in d.warnings)

        d2 = etl.EtlDesign(target_table="dws_trade_daily",
                           etl_sql="INSERT OVERWRITE TABLE dws_trade_daily SELECT SUM(x) FROM d GROUP BY dt")
        etl.validate_design(d2, {"dws_trade_daily"}, "hive", "汇总")
        assert any("同名表" in w for w in d2.warnings)

    def test_agg_without_group_by(self):
        d = etl.EtlDesign(target_table="dws_a", etl_sql="INSERT OVERWRITE TABLE dws_a SELECT SUM(gmv) FROM dwd_b")
        etl.validate_design(d, set(), "hive", "汇总GMV")
        assert any("GROUP BY" in w for w in d.warnings)

    def test_dangerous_sql_stripped(self):
        d = etl.EtlDesign(target_table="dws_a",
                          ddl="CREATE TABLE dws_a (x int); DROP TABLE dwd_b;",
                          etl_sql="INSERT OVERWRITE TABLE dws_a SELECT x FROM dwd_b GROUP BY x")
        etl.validate_design(d, set(), "mysql", "加工")
        assert "DROP" not in d.ddl.upper()
        assert "CREATE TABLE" in d.ddl.upper().replace("  ", " ")
        assert any("危险语句" in w for w in d.warnings)

    def test_partition_var_suggestion(self):
        d = etl.EtlDesign(target_table="dws_a",
                          etl_sql="INSERT OVERWRITE TABLE dws_a SELECT dt, SUM(gmv) FROM dwd GROUP BY dt")
        etl.validate_design(d, set(), "hive", "汇总")
        assert any("${ds}" in w for w in d.warnings)

    async def test_etl_design_without_llm(self, session):
        """LLM 不可用时仍返回源表推荐 + 明确告警，不崩溃。"""
        ds = _ds()
        session.add(ds)
        await session.flush()
        await _seed_tables(session, ds)
        design = await etl.etl_design(session=session, question="帮我新建一张按天汇总GMV的表",
                                      datasource=ds)
        assert design.warnings  # LLM 生成失败的告警
        assert design.dialect == "mysql"
        md = design.to_markdown()
        assert "系统不会自动执行" in md


class TestAzkabanPackage:
    def test_flow_zip_contents(self):
        zip_bytes, flow = azk.build_flow_zip(
            target_table="dws_trade_daily", etl_sql="INSERT OVERWRITE TABLE dws_trade_daily PARTITION(dt='${ds}') SELECT 1",
            ddl="CREATE TABLE dws_trade_daily (gmv double)", dialect="hive", notes="口径说明")
        assert flow == "dws_trade_daily"
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        names = set(zf.namelist())
        assert {"flow20.project", "dws_trade_daily.flow", "etl.sql", "DDL.sql", "README.md"} <= names
        flow_yaml = zf.read("dws_trade_daily.flow").decode()
        assert "type: command" in flow_yaml and "beeline" in flow_yaml
        assert "${ds}" in zf.read("etl.sql").decode()      # 分区变量保留占位
        assert "sed" in flow_yaml                            # 执行时展开 ${ds}
        # 凭证不进 zip
        assert "password" not in zf.read("dws_trade_daily.flow").decode().lower()

    def test_flow_name_sanitized(self):
        _, flow = azk.build_flow_zip(target_table=" weird 表 name!!", etl_sql="x",
                                     ddl="", dialect="mysql")
        assert flow and all(ch.isalnum() or ch in "_-" for ch in flow)


# ================= 自定义 Skill =================

def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, c in files.items():
            z.writestr(n, c)
    return buf.getvalue()


class TestSkillPackageParse:
    def test_valid_prompt_package(self):
        pkg = cs.parse_package(_zip({
            "skill.yaml": 'name: weekly\ndescription: 周报\ntype: prompt\ntrigger_keywords: ["周报"]\n',
            "prompt.md": "请基于 ${kg_context} 生成周报，问题：${question}",
        }))
        assert pkg.name == "weekly" and pkg.skill_type == "prompt"
        assert pkg.trigger_keywords == ["周报"]

    def test_missing_manifest_rejected(self):
        with pytest.raises(Exception, match="skill.yaml"):
            cs.parse_package(_zip({"prompt.md": "hi"}))

    def test_python_blacklist_rejected(self):
        with pytest.raises(Exception, match="危险调用"):
            cs.parse_package(_zip({
                "skill.yaml": "name: hack\ntype: python\ntrigger_keywords: ['x']\n",
                "main.py": "import os\ndef run(question, ctx):\n    os.system('rm -rf /')\n    return ''\n",
            }))

    def test_python_needs_run_entry(self):
        with pytest.raises(Exception, match="def run"):
            cs.parse_package(_zip({
                "skill.yaml": "name: bad\ntype: python\ntrigger_keywords: ['x']\n",
                "main.py": "print('no entry')\n",
            }))

    def test_traversal_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("../evil.yaml", "name: x")
        with pytest.raises(Exception):
            cs.parse_package(buf.getvalue())


class TestSkillSandbox:
    async def test_python_skill_runs(self):
        files = {
            "main.py": "def run(question, ctx):\n"
                       "    return f\"共 {len(ctx['tables'])} 张表，问题：{question}\"\n",
        }
        r = await cs.run_python_skill(files, "main.py", "有多少表",
                                      {"tables": [{"name": "t1"}, {"name": "t2"}]})
        assert r["ok"], r
        assert "2 张表" in r["result"]

    async def test_python_skill_error_reported(self):
        files = {"main.py": "def run(question, ctx):\n    raise ValueError('boom')\n"}
        r = await cs.run_python_skill(files, "main.py", "q", {})
        assert not r["ok"] and "boom" in r["error"]

    def test_render_prompt(self):
        out = cs.render_prompt("Q=${question} DS=${datasource}",
                               question="周报", datasource="主仓", kg_context="ctx")
        assert out == "Q=周报 DS=主仓"


# ================= 指标口径问答 / 指标查表引导（PRD 3.3-7） =================

class TestMetricQa:
    async def _seed_metric(self, session, ds, t1):
        import uuid as _uuid
        m = KgMetric(name=f"GMV_{_uuid.uuid4().hex[:6]}", aliases=["销售额", "成交额"],
                     business_def="含税成交总金额（不含退款）",
                     formula="SUM(gmv)", agg_function="sum", unit="元",
                     period_grain="day", business_domain="交易",
                     table_id=t1.id, status=MetricStatus.ACTIVE,
                     source=SourceKind.MANUAL)
        session.add(m)
        await session.commit()
        return m

    async def test_match_metrics_by_alias(self, session):
        ds = _ds()
        session.add(ds)
        await session.flush()
        t1, _ = await _seed_tables(session, ds)
        await self._seed_metric(session, ds, t1)
        hits = await table_qa.match_metrics(session, ds.id, "销售额的口径是什么")
        assert len(hits) == 1 and hits[0].aliases[0] == "销售额"
        assert not await table_qa.match_metrics(session, ds.id, "今天的天气怎么样")

    async def test_metric_card_content(self, session):
        ds = _ds()
        session.add(ds)
        await session.flush()
        t1, _ = await _seed_tables(session, ds)
        m = await self._seed_metric(session, ds, t1)
        cards = await table_qa.metric_cards(session, [m])
        c = cards[0]
        assert c["business_def"] == "含税成交总金额（不含退款）"
        assert c["formula"] == "SUM(gmv)" and c["unit"] == "元"
        # 可查表反查成功且带分层
        assert c["tables"][0]["name"] == "dws_trade_daily"
        assert c["tables"][0]["layer"] == "dws"

    async def test_context_has_metric_section(self, session):
        ds = _ds()
        session.add(ds)
        await session.flush()
        t1, _ = await _seed_tables(session, ds)
        await self._seed_metric(session, ds, t1)
        ctx, matched, cards = await table_qa.build_context(session, ds.id, "销售额的口径是什么")
        assert "命中指标（口径卡" in ctx
        assert "含税成交总金额" in ctx and "SUM(gmv)" in ctx
        assert "可查表" in ctx and "dws_trade_daily" in ctx
        assert cards and cards[0]["aliases"][0] == "销售额"

    async def test_answer_yields_cards(self, session, monkeypatch):
        """口径问答：LLM 流式回答 + 口径卡随流产出；未登记口径时上下文如实标注。"""
        ds = _ds()
        session.add(ds)
        await session.flush()
        t1, _ = await _seed_tables(session, ds)
        m = await self._seed_metric(session, ds, t1)
        m.business_def, m.formula = "", ""   # 口径未登记
        await session.commit()

        from app.llm.types import StreamChunk
        class G:
            async def chat_stream(self, scene, messages, **kw):
                assert "口径未登记" in messages[1].content or "未登记" in messages[1].content
                yield StreamChunk(delta="该指标口径未登记，请到「知识图谱」页补充。")
        monkeypatch.setattr(table_qa, "gateway", G())

        text, cards = "", []
        async for delta, matched, cds in table_qa.table_qa_answer(
                session=session, question="销售额的口径是什么", datasource_id=ds.id):
            text += delta
            cards = cds
        assert "未登记" in text
        card = next(c for c in cards if c["name"] == m.name)
        assert card["business_def"] == ""
