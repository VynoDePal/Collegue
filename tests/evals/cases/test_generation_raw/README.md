# test_generation_raw — baseline cases

Mirror of [../test_generation/](../test_generation/). Identical `code:` field on
purpose — the whole point of this directory is a **fair apples-to-apples
comparison** between the MCP `test_generation` tool and a plain "please write
tests for this code" request to the same LLM.

If you add a case here, add the same one to `../test_generation/` so the
matrix report stays symmetric. If you deliberately want to test a scenario
that only makes sense without the MCP tool's framing, keep it here only and
the MCP column will show em-dash in the matrix report.
