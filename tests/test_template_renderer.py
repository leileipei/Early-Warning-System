import pytest

from app.template_renderer import TemplateRenderError, render_per_row, render_summary


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


def test_render_per_row_uses_current_row():
    message = render_per_row(
        subject_template="订单 {{id}}",
        body_template="金额 {{amount}}",
        row={"id": 9, "amount": 30000},
        context={},
    )

    assert message.subject == "订单 9"
    assert message.html_body == "金额 30000"


def test_missing_field_raises_render_error():
    with pytest.raises(TemplateRenderError):
        render_per_row("订单 {{missing}}", "body", {"id": 1}, {})


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
