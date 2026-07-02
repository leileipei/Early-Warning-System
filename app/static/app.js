document.addEventListener("DOMContentLoaded", () => {
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
});
