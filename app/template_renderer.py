from dataclasses import dataclass
from html import escape

from jinja2 import StrictUndefined, Template, TemplateError
from markupsafe import Markup


class TemplateRenderError(ValueError):
    pass


@dataclass(frozen=True)
class RenderedMessage:
    subject: str
    html_body: str


def _render(template_text: str, context: dict) -> str:
    try:
        return Template(template_text, undefined=StrictUndefined, autoescape=True).render(**context)
    except TemplateError as exc:
        raise TemplateRenderError(str(exc)) from exc


def _table(rows: list[dict]) -> str:
    if not rows:
        return "<table></table>"

    columns = list(rows[0].keys())
    header = "".join(f"<th>{escape(str(column))}</th>" for column in columns)
    body = ""
    for row in rows:
        cells = "".join(f"<td>{escape(str(row.get(column, '')))}</td>" for column in columns)
        body += f"<tr>{cells}</tr>"

    return f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>"


def render_summary(
    subject_template: str,
    body_template: str,
    rows: list[dict],
    context: dict,
) -> RenderedMessage:
    merged = {**context, "table": Markup(_table(rows))}
    return RenderedMessage(
        subject=_render(subject_template, merged),
        html_body=_render(body_template, merged),
    )


def render_per_row(
    subject_template: str,
    body_template: str,
    row: dict,
    context: dict,
) -> RenderedMessage:
    merged = {**context, **row}
    return RenderedMessage(
        subject=_render(subject_template, merged),
        html_body=_render(body_template, merged),
    )
