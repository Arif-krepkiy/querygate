"""Snapshot of the public MCP surface: tools, prompts, resources, annotations."""

from __future__ import annotations

import pytest

from querygate.api import tools as tools_module

EXPECTED_TOOLS = {
    "qg_search_catalog",
    "qg_describe_model",
    "qg_get_filter_values",
    "qg_get_table_stats",
    "qg_get_metric",
    "qg_run_query",
    "qg_reload_catalog",
}


@pytest.fixture(scope="module")
async def listed_tools():
    return await tools_module.mcp.list_tools()


class TestTools:
    async def test_surface_is_exactly_this(self, listed_tools):
        assert {t.name for t in listed_tools} == EXPECTED_TOOLS

    async def test_read_tools_are_annotated_read_only(self, listed_tools):
        """Clients use these hints to decide what may run without confirmation."""
        for tool in listed_tools:
            if tool.name == "qg_reload_catalog":
                continue
            assert tool.annotations.readOnlyHint is True, tool.name
            assert tool.annotations.destructiveHint is False, tool.name

    async def test_no_tool_is_destructive(self, listed_tools):
        """Read-only is a property of the whole server, not of most of it."""
        assert all(t.annotations.destructiveHint is False for t in listed_tools)

    async def test_every_tool_documents_itself(self, listed_tools):
        """Descriptions are the model's only guidance on when to call a tool."""
        for tool in listed_tools:
            assert tool.description and len(tool.description) > 80, tool.name

    async def test_run_query_exposes_the_expected_inputs(self, listed_tools):
        run_query = next(t for t in listed_tools if t.name == "qg_run_query")
        properties = run_query.inputSchema["properties"]
        assert {"sql", "row_limit", "dry_run", "cursor"} <= set(properties)

    async def test_row_limit_bounds_come_from_config(self, listed_tools):
        """Limits are configuration, and the schema must reflect the deployment."""
        from querygate import config

        run_query = next(t for t in listed_tools if t.name == "qg_run_query")
        row_limit = run_query.inputSchema["properties"]["row_limit"]
        assert row_limit["maximum"] == config.MAX_ROW_LIMIT

    async def test_sql_input_is_length_capped(self, listed_tools):
        run_query = next(t for t in listed_tools if t.name == "qg_run_query")
        assert run_query.inputSchema["properties"]["sql"]["maxLength"] > 0


class TestPrompts:
    async def test_prompts_are_offered(self):
        names = {p.name for p in await tools_module.mcp.list_prompts()}
        assert {"explore_domain", "answer_question", "data_dictionary", "sanity_check"} <= names

    async def test_prompts_declare_their_arguments(self):
        prompts = {p.name: p for p in await tools_module.mcp.list_prompts()}
        assert [a.name for a in prompts["explore_domain"].arguments] == ["topic"]

    async def test_prompt_renders_with_the_users_input(self):
        result = await tools_module.mcp.get_prompt("answer_question", {"question": "revenue by region?"})
        assert "revenue by region?" in result.messages[0].content.text


class TestResources:
    async def test_llm_instructions_are_published(self):
        uris = {str(r.uri) for r in await tools_module.mcp.list_resources()}
        assert "querygate://llm-instructions" in uris

    async def test_catalog_overview_is_published(self):
        uris = {str(r.uri) for r in await tools_module.mcp.list_resources()}
        assert "querygate://catalog" in uris

    async def test_schema_template_is_offered(self):
        """A resource template lets a client pin one model's schema to the
        conversation without spending a tool call."""
        templates = await tools_module.mcp.list_resource_templates()
        assert "schema://{model}" in {t.uriTemplate for t in templates}

    async def test_server_instructions_are_non_empty(self):
        """Sent on initialize: the model's standing brief for the session."""
        assert tools_module.mcp.instructions
        assert "governed" in tools_module.mcp.instructions.lower()
