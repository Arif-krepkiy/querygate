from __future__ import annotations

from mcp.server.fastmcp.prompts import base


def register(mcp) -> None:
    @mcp.prompt(title="Explore a data domain")
    def explore_domain(topic: str) -> list[base.Message]:
        """Survey what data exists on a topic before asking anything specific."""
        return [
            base.UserMessage(
                f"I want to understand what data we have about {topic}.\n\n"
                f"Search the catalog, then for the two or three most relevant models "
                f"look at their columns and profile them. Tell me in plain language: "
                f"what questions could I actually answer with this, what time range "
                f"does it cover, and anything that looks like a gotcha (columns that "
                f"are mostly empty, or definitions that might not mean what I assume).\n\n"
                f"Don't show me SQL or table names unless I ask; describe it the way "
                f"you'd describe it to a colleague."
            )
        ]

    @mcp.prompt(title="Answer a business question")
    def answer_question(question: str) -> list[base.Message]:
        """The standard path: ground, check real values, run, answer in business terms."""
        return [
            base.UserMessage(
                f"{question}\n\n"
                f"Work it out from the warehouse: find the right model, check the real "
                f"values for anything you filter on rather than guessing them, and if a "
                f"defined metric covers this, use its definition rather than your own. "
                f"Then give me the answer in plain language: the number and one "
                f"sentence about what was counted."
            )
        ]

    @mcp.prompt(title="Build a data dictionary")
    def data_dictionary(domain: str = "") -> list[base.Message]:
        """Turn catalog metadata into something a human can read."""
        scope = f"the {domain} domain" if domain else "the main models"
        return [
            base.UserMessage(
                f"Produce a data dictionary for {scope}.\n\n"
                f"For each model: what it represents, its grain (what one row means), "
                f"the columns that matter with their meaning, and how it joins to the "
                f"others. Use the declared joins rather than inferring them from column "
                f"names. Flag anything undocumented: a column with no description is a "
                f"finding worth reporting, not a blank to fill in with a guess.\n\n"
                f"Format it as markdown I could paste into our team wiki."
            )
        ]

    @mcp.prompt(title="Sanity-check a number")
    def sanity_check(claim: str) -> list[base.Message]:
        """Verify a figure someone is about to put in a deck."""
        return [
            base.UserMessage(
                f'Someone reported: "{claim}"\n\n'
                f"Check it against the warehouse. Compute it yourself, then tell me "
                f"whether it holds up. If your number differs, the interesting part is "
                f"*why*: a different definition, a different date range, or filters "
                f"like cancelled or refunded rows being included. Say which."
            )
        ]
