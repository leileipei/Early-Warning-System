document.addEventListener("DOMContentLoaded", () => {
  const escapeHtml = (value) =>
    String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");

  document.querySelectorAll("[data-sql-check-button]").forEach((button) => {
    button.addEventListener("click", async () => {
      const form = button.closest("form");
      const endpoint = button.dataset.endpoint;
      const sqlInput = form?.querySelector("[data-sql-input]");
      const dataSourceInput = form?.querySelector('[name="data_source_id"]');
      const result = form?.querySelector("[data-sql-check-result]");
      if (!endpoint || !sqlInput || !result) {
        return;
      }

      button.disabled = true;
      result.textContent = "检测中...";
      result.classList.remove("is-success", "is-error");

      const payload = new FormData();
      payload.append("sql_text", sqlInput.value);
      payload.append("data_source_id", dataSourceInput?.value || "");
      const csrfInput = form?.querySelector('[name="_csrf_token"]');
      payload.append("_csrf_token", csrfInput?.value || "");

      try {
        const response = await fetch(endpoint, {
          method: "POST",
          body: payload,
          headers: {
            Accept: "application/json",
          },
        });
        const data = await response.json();
        result.textContent = data.message || "SQL 检测失败";
        result.classList.toggle("is-success", response.ok);
        result.classList.toggle("is-error", !response.ok);
      } catch (_error) {
        result.textContent = "SQL 检测失败，请稍后重试";
        result.classList.add("is-error");
      } finally {
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-sql-preview-button]").forEach((button) => {
    button.addEventListener("click", async () => {
      const form = button.closest("form");
      const endpoint = button.dataset.endpoint;
      const sqlInput = form?.querySelector("[data-sql-input]");
      const dataSourceInput = form?.querySelector('[name="data_source_id"]');
      const timeoutInput = form?.querySelector('[name="query_timeout_seconds"]');
      const result = form?.querySelector("[data-sql-preview-result]");
      if (!endpoint || !sqlInput || !result) {
        return;
      }

      button.disabled = true;
      result.className = "preview-panel";
      result.textContent = "预览中...";

      const payload = new FormData();
      payload.append("sql_text", sqlInput.value);
      payload.append("data_source_id", dataSourceInput?.value || "");
      payload.append("query_timeout_seconds", timeoutInput?.value || "30");
      const csrfInput = form?.querySelector('[name="_csrf_token"]');
      payload.append("_csrf_token", csrfInput?.value || "");

      try {
        const response = await fetch(endpoint, {
          method: "POST",
          body: payload,
          headers: {
            Accept: "application/json",
          },
        });
        const data = await response.json();
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
    });
  });
});
