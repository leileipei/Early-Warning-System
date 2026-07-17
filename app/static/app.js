document.addEventListener("DOMContentLoaded", () => {
  const escapeHtml = (value) =>
    String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");

  const formPayload = (form, fields) => {
    const payload = new FormData();
    for (const [name, value] of fields) {
      payload.append(name, value?.value || "");
    }
    return payload;
  };

  const fetchSqlResult = async (endpoint, payload) => {
    const response = await fetch(endpoint, {
      method: "POST",
      body: payload,
      headers: { Accept: "application/json" },
    });
    return { response, data: await response.json() };
  };

  const validateSql = async (button) => {
    const form = button.closest("form");
    const endpoint = button.dataset.endpoint;
    const sqlInput = form?.querySelector("[data-sql-input]");
    const dataSourceInput = form?.querySelector('[name="data_source_id"]');
    const result = form?.querySelector("[data-sql-check-result]");
    if (!form || !endpoint || !sqlInput || !result) {
      return;
    }

    button.disabled = true;
    result.textContent = "检测中...";
    result.classList.remove("is-success", "is-error");

    try {
      const payload = formPayload(form, [
        ["sql_text", sqlInput],
        ["data_source_id", dataSourceInput],
      ]);
      const csrfInput = form.querySelector('[name="_csrf_token"]');
      payload.append("_csrf_token", csrfInput?.value || "");
      const { response, data } = await fetchSqlResult(
        endpoint,
        payload,
      );
      result.textContent = data.message || "SQL 检测失败";
      result.classList.toggle("is-success", response.ok);
      result.classList.toggle("is-error", !response.ok);
    } catch (_error) {
      result.textContent = "SQL 检测失败，请稍后重试";
      result.classList.add("is-error");
    } finally {
      button.disabled = false;
    }
  };

  const previewSql = async (button) => {
    const form = button.closest("form");
    const endpoint = button.dataset.endpoint;
    const sqlInput = form?.querySelector("[data-sql-input]");
    const dataSourceInput = form?.querySelector('[name="data_source_id"]');
    const timeoutInput = form?.querySelector('[name="query_timeout_seconds"]');
    const result = form?.querySelector("[data-sql-preview-result]");
    if (!form || !endpoint || !sqlInput || !result) {
      return;
    }

    button.disabled = true;
    result.className = "preview-panel";
    result.textContent = "预览中...";

    try {
      const payload = formPayload(form, [
        ["sql_text", sqlInput],
        ["data_source_id", dataSourceInput],
        ["query_timeout_seconds", timeoutInput],
      ]);
      const csrfInput = form.querySelector('[name="_csrf_token"]');
      payload.append("_csrf_token", csrfInput?.value || "");
      const { response, data } = await fetchSqlResult(
        endpoint,
        payload,
      );
      if (!response.ok) {
        result.classList.add("is-error");
        result.textContent = data.message || "预览失败";
        return;
      }

      result.classList.add("is-success");
      if (!data.rows?.length) {
        result.textContent = data.message || "查询成功，暂无结果";
        return;
      }

      const headers = data.columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("");
      const rows = data.rows
        .map((row) => {
          const cells = data.columns
            .map((column) => `<td>${escapeHtml(row[column])}</td>`)
            .join("");
          return `<tr>${cells}</tr>`;
        })
        .join("");
      result.innerHTML = `
        <p>${escapeHtml(data.message || "查询成功")}</p>
        <div class="table-shell">
          <table class="preview-table">
            <thead><tr>${headers}</tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      `;
    } catch (_error) {
      result.classList.add("is-error");
      result.textContent = "预览失败，请稍后重试";
    } finally {
      button.disabled = false;
    }
  };

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.dataset.confirm) {
      return;
    }
    if (!window.confirm(form.dataset.confirm)) {
      event.preventDefault();
    }
  });

  document.addEventListener("click", async (event) => {
    if (!(event.target instanceof Element)) {
      return;
    }
    const button = event.target.closest("[data-sql-check-button], [data-sql-preview-button]");
    if (!(button instanceof HTMLButtonElement)) {
      return;
    }
    if (button.matches("[data-sql-check-button]")) {
      await validateSql(button);
    } else {
      await previewSql(button);
    }
  });
});
