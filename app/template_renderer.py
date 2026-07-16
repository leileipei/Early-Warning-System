from dataclasses import dataclass
from html import escape

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import ImmutableSandboxedEnvironment
from markupsafe import Markup


class TemplateRenderError(ValueError):
    pass


@dataclass(frozen=True)
class RenderedMessage:
    subject: str
    html_body: str


def _build_environment(*, autoescape: bool) -> ImmutableSandboxedEnvironment:
    environment = ImmutableSandboxedEnvironment(
        undefined=StrictUndefined,
        autoescape=autoescape,
    )
    environment.globals.clear()
    return environment


_SUBJECT_ENVIRONMENT = _build_environment(autoescape=False)
_BODY_ENVIRONMENT = _build_environment(autoescape=True)


def _render(template_text: str, context: dict, *, autoescape: bool) -> str:
    environment = _BODY_ENVIRONMENT if autoescape else _SUBJECT_ENVIRONMENT
    try:
        return environment.from_string(template_text).render(**context)
    except TemplateError as exc:
        raise TemplateRenderError(str(exc)) from exc


def _render_subject(template_text: str, context: dict) -> str:
    return _render(template_text, context, autoescape=False)


def _render_html_body(template_text: str, context: dict) -> str:
    return _render(template_text, context, autoescape=True)


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
    body_context = {**context, "table": Markup(_table(rows))}
    return RenderedMessage(
        subject=_render_subject(subject_template, context),
        html_body=_render_html_body(body_template, body_context),
    )


def render_per_row(
    subject_template: str,
    body_template: str,
    row: dict,
    context: dict,
) -> RenderedMessage:
    merged = {**context, **row}
    return RenderedMessage(
        subject=_render_subject(subject_template, merged),
        html_body=_render_html_body(body_template, merged),
    )
