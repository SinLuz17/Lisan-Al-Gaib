"""Per-target schema rules.

Each profile is a snapshot of what one provider's API accepts for tool
schemas, taken from its own documentation plus bug reports with reproductions.
Three levels, deliberately not two:

* ``rejects``   -- the API fails the request (documented, with a source).
* ``silent``    -- the keyword survives the request but the constraint is not
                   enforced, or a gateway drops it on the way. No error, ever.
* ``unverified``-- neither documented as supported nor as rejected. The tool
                   keeps the keyword and says so; guessing here is how teams
                   ship a schema nobody ever tested.

Sources live next to the rules so a wrong profile is traceable to a page
instead of to memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

# Keywords that constrain values rather than shape. These are the ones that
# vanish quietly: the JSON still validates, the model is simply unconstrained.
CONSTRAINT_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    }
)

# Keywords that describe composition or identity. Losing one of these changes
# which shape is legal, not just how large a value may be.
STRUCTURAL_KEYWORDS = frozenset(
    {
        "$ref",
        "$defs",
        "definitions",
        "additionalProperties",
        "patternProperties",
        "propertyNames",
        "not",
        "if",
        "then",
        "else",
        "dependentRequired",
        "dependentSchemas",
        "dependencies",
    }
)


@dataclass(frozen=True)
class Profile:
    key: str
    label: str
    #: Target requires every object closed and every property listed in
    #: ``required`` (OpenAI strict mode).
    strict_object: bool
    #: Target can be handed a closed object (``additionalProperties: false``).
    accepts_additional_properties: bool
    #: Target accepts local ``$ref``/``$defs``; otherwise they get inlined.
    accepts_ref: bool
    #: Target wants string-only enum values.
    string_enums_only: bool
    rejects: frozenset
    silent: frozenset
    unverified: frozenset
    max_props: Optional[int]
    max_depth: Optional[int]
    max_union: Optional[int]
    notes: Tuple[str, ...]
    sources: Tuple[Tuple[str, str], ...]


_OPENAI_SOURCES = (
    (
        "Structured Outputs / strict mode requirements",
        "https://platform.openai.com/docs/guides/structured-outputs",
    ),
    (
        "Constraint grid across providers, Nov 2026",
        "https://www.toolbit.ai/blog/llm-structured-output-json-schema",
    ),
)

_GEMINI_SOURCES = (
    (
        "Function declaration schema is an OpenAPI subset; $ref/$defs/"
        "additionalProperties return HTTP 400 with minimal repro",
        "https://github.com/deepseek-ai/deepseek-harness/discussions/10904",
    ),
    (
        "Non-string enums and nested anyOf rejected in practice",
        "https://community.n8n.io/t/gemini-cant-read-n8n-mcp-tools/326400/5",
    ),
    (
        "Keyword list Gemini documents, Nov 2026",
        "https://www.toolbit.ai/blog/llm-structured-output-json-schema",
    ),
)


PROFILES = {
    "openai": Profile(
        key="openai",
        label="OpenAI (strict function calling)",
        strict_object=True,
        accepts_additional_properties=True,
        accepts_ref=True,
        string_enums_only=False,
        rejects=frozenset(),
        silent=frozenset(),
        unverified=CONSTRAINT_KEYWORDS,
        max_props=None,
        max_depth=None,
        max_union=None,
        notes=(
            "strict 模式要求每个 object 都写 additionalProperties: false，且所有字段进 required",
            "递归 $ref 支持；非字符串 enum 支持",
        ),
        sources=_OPENAI_SOURCES,
    ),
    "azure": Profile(
        key="azure",
        label="Azure OpenAI (same shape, tighter keyword list)",
        strict_object=True,
        accepts_additional_properties=True,
        accepts_ref=True,
        string_enums_only=False,
        rejects=frozenset(),
        silent=CONSTRAINT_KEYWORDS,
        unverified=frozenset(),
        max_props=100,
        max_depth=5,
        max_union=None,
        notes=(
            "根节点不能是 anyOf；object 属性总数上限 100，嵌套上限 5 层",
            "minimum/maximum/pattern/format 等关键字文档列为不支持：请求不报错，约束不生效",
        ),
        sources=_OPENAI_SOURCES,
    ),
    "anthropic": Profile(
        key="anthropic",
        label="Anthropic Messages (input_schema)",
        strict_object=False,
        accepts_additional_properties=True,
        accepts_ref=True,
        string_enums_only=False,
        rejects=frozenset(),
        silent=frozenset(),
        unverified=CONSTRAINT_KEYWORDS
        | {"patternProperties", "propertyNames", "dependentRequired"},
        max_props=None,
        max_depth=None,
        max_union=16,
        notes=(
            "文档只承诺一个 JSON Schema 子集；有实测说范围/长度类关键字会被拒，未逐条复核，故列为 unverified",
            "union 分支上限 16；不支持递归 schema",
        ),
        sources=(
            (
                "Constraint grid: Claude rejects range and length keywords",
                "https://www.toolbit.ai/blog/llm-structured-output-json-schema",
            ),
        ),
    ),
    "gemini": Profile(
        key="gemini",
        label="Gemini functionDeclarations (OpenAPI subset)",
        strict_object=False,
        accepts_additional_properties=False,
        accepts_ref=False,
        string_enums_only=True,
        rejects=frozenset(
            {
                "$ref",
                "$defs",
                "definitions",
                "additionalProperties",
                "patternProperties",
                "propertyNames",
                "not",
                "if",
                "then",
                "else",
                "dependentRequired",
                "dependentSchemas",
                "dependencies",
            }
        ),
        silent=frozenset(),
        unverified=frozenset(
            {
                "oneOf",
                "anyOf",
                "const",
                "exclusiveMinimum",
                "exclusiveMaximum",
                "uniqueItems",
                "minProperties",
                "maxProperties",
                "format",
            }
        ),
        max_props=None,
        max_depth=None,
        max_union=None,
        notes=(
            "未知关键字直接 400（Unknown name ...），不是忽略",
            "enum 值必须是字符串；null 用 type 数组表达",
        ),
        sources=_GEMINI_SOURCES,
    ),
}


def get(key: str) -> Profile:
    try:
        return PROFILES[key]
    except KeyError:
        raise KeyError(
            "unknown profile %r (known: %s)" % (key, ", ".join(sorted(PROFILES)))
        )


def all_keys() -> Tuple[str, ...]:
    """Canonical order: the two strict-mode targets first, then the looser ones."""
    return tuple(key for key in ("openai", "azure", "anthropic", "gemini") if key in PROFILES)
