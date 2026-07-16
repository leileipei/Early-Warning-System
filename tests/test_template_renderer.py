from pathlib import Path
import tomllib

from packaging.requirements import Requirement
from packaging.version import Version
import pytest

from app.template_renderer import TemplateRenderError, render_per_row, render_summary


def _has_safe_jinja_version_range(requirement: Requirement) -> bool:
    minimum_safe_version = Version("3.1.6")
    declared_lower_bounds = [
        Version(specifier.version)
        for specifier in requirement.specifier
        if specifier.operator in {">", ">=", "~="}
    ]

    return bool(declared_lower_bounds) and max(declared_lower_bounds) >= minimum_safe_version


@pytest.mark.parametrize(
    ("requirement_text", "expected"),
    [
        ("jinja2>=3.1.6", True),
        ("jinja2~=3.1.6", True),
        ("jinja2>=3.2", True),
        ("jinja2>3.1.5", False),
        ("jinja2>=3.1", False),
        ("jinja2>=3.1.6,<4", True),
    ],
)
def test_safe_jinja_version_range(requirement_text, expected):
    assert _has_safe_jinja_version_range(Requirement(requirement_text)) is expected


def test_jinja_dependency_requires_patched_release():
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    dependencies = [
        Requirement(dependency) for dependency in pyproject["project"]["dependencies"]
    ]
    jinja_dependency = next(
        dependency for dependency in dependencies if dependency.name.lower() == "jinja2"
    )

    assert _has_safe_jinja_version_range(jinja_dependency)


def test_render_summary_includes_html_table():
    message = render_summary(
        subject_template="预警 {{rule_name}}",
        body_template="<p>{{rule_name}}</p>{{table}}",
        rows=[{"id": 1, "amount": 12000}, {"id": 2, "amount": 15000}],
        context={"rule_name": "大额订单"},
    )

    assert message.subject == "预警 大额订单"
    assert "<table" in message.html_body
    assert "12000" in message.html_body


def test_render_summary_subject_uses_context_table_but_body_uses_generated_table():
    message = render_summary(
        subject_template="{{table}}",
        body_template="{{table}}",
        rows=[{"id": 1, "amount": 12000}],
        context={"table": "subject-table"},
    )

    assert message.subject == "subject-table"
    assert "<table" in message.html_body
    assert "12000" in message.html_body
    assert message.html_body != "subject-table"


def test_render_per_row_uses_current_row():
    message = render_per_row(
        subject_template="订单 {{id}}",
        body_template="金额 {{amount}}",
        row={"id": 9, "amount": 30000},
        context={},
    )

    assert message.subject == "订单 9"
    assert message.html_body == "金额 30000"


def test_subject_renders_as_plain_text_without_html_escape():
    message = render_per_row(
        subject_template="预警 {{name}}",
        body_template="body",
        row={"name": "A & <B>"},
        context={},
    )

    assert message.subject == "预警 A & <B>"


def test_body_escapes_regular_template_variables():
    message = render_summary(
        subject_template="预警",
        body_template="<p>{{rule_name}}</p>",
        rows=[],
        context={"rule_name": "<b>x</b>"},
    )

    assert "&lt;b&gt;x&lt;/b&gt;" in message.html_body
    assert "<p><b>x</b></p>" not in message.html_body


def test_missing_field_raises_render_error():
    with pytest.raises(TemplateRenderError):
        render_per_row("订单 {{missing}}", "body", {"id": 1}, {})


def test_missing_body_field_raises_render_error():
    with pytest.raises(TemplateRenderError):
        render_per_row("订单 {{id}}", "金额 {{missing}}", {"id": 1}, {})


def test_summary_table_escapes_header_and_cell_values():
    message = render_summary(
        subject_template="预警",
        body_template="{{table}}",
        rows=[{"<id>": "<script>alert(1)</script>", "amount": 12000}],
        context={},
    )

    assert "&lt;id&gt;" in message.html_body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in message.html_body
    assert "<script>" not in message.html_body


def test_missing_context_field_raises_render_error():
    with pytest.raises(TemplateRenderError):
        render_summary("预警 {{rule_name}}", "{{table}}", [{"id": 1}], {})


def test_summary_table_uses_columns_from_first_row():
    message = render_summary(
        subject_template="预警",
        body_template="{{table}}",
        rows=[
            {"id": 1, "amount": 12000},
            {"id": 2, "amount": 15000, "ignored": "not rendered"},
        ],
        context={},
    )

    assert "<th>id</th>" in message.html_body
    assert "<th>amount</th>" in message.html_body
    assert "not rendered" not in message.html_body


def test_template_renderer_does_not_expose_default_jinja_globals():
    with pytest.raises(TemplateRenderError):
        render_per_row("预警", "{{ cycler }}", {"id": 1}, {"rule_name": "测试"})


def test_template_renderer_rejects_unsafe_python_access():
    with pytest.raises(TemplateRenderError):
        render_per_row(
            "预警",
            "{{ ''.__class__.__mro__ }}",
            {"id": 1},
            {"rule_name": "测试"},
        )


def test_template_renderer_rejects_attr_filter_format_sandbox_escape():
    template_text = (
        '{{ "{0.__call__.__builtins__[__import__]}" | attr("format")(not_here) }}'
    )

    with pytest.raises(TemplateRenderError):
        render_per_row("预警", template_text, {"id": 1}, {"rule_name": "测试"})


def test_template_renderer_keeps_safe_conditions_and_loops():
    rendered = render_summary(
        "{{ rule_name }}",
        "{% if row_count %}{% for row in rows %}{{ row.id }}{% endfor %}{% endif %}",
        [{"id": 1}, {"id": 2}],
        {"rule_name": "测试", "row_count": 2, "rows": [{"id": 1}, {"id": 2}]},
    )

    assert rendered.subject == "测试"
    assert rendered.html_body == "12"
