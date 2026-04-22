# test_generation_competent — "skilled user" baseline cases

Mirror of [../test_generation/](../test_generation/). Same `code:` field as both
sibling directories, on purpose. The three directories correspond to three
prompt strategies that all see the exact same input code :

| Directory | Who writes the prompt |
|---|---|
| `test_generation/` | MCP Collègue `TestGenerationTool` (tuned prompt engineering) |
| `test_generation_raw/` | Naive user ("write pytest tests for this code") |
| `test_generation_competent/` | Developer who knows pytest (asks for edge cases, parametrize, pytest.raises, etc.) |

The point of adding this third axis is to answer the honest question : **is
the MCP tool actually better than a mid-level developer's prompt, or just
better than a beginner's?** If the Δ MCP − competent is large, the tool
earns its complexity. If it's ~0, the value is mostly "saving the user from
writing a careful prompt themselves".

Keep the three directories in sync : adding a case means adding it in all
three. Only create a divergence if you deliberately want to test a prompt
strategy that can't work in one of the paths.
