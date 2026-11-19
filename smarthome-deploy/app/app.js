"use strict";
const button = document.getElementById("refresh");
const summary = document.getElementById("summary");
const version = document.getElementById("version");
const files = document.getElementById("files");
let currentCommit = "";
async function refresh() {
  button.disabled = true;
  summary.textContent = "Сравниваем файлы…";
  summary.className = "";
  version.textContent = "";
  files.replaceChildren();
  try {
    const response = await fetch("api/status", { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Ошибка проверки");
    if (!result.configured) {
      summary.textContent = result.message || "Настройте GitHub";
      return;
    }
    currentCommit = result.commit;
    const t = result.totals;
    summary.textContent = `Совпадают: ${t.same} · Различаются: ${t.modified} · Отсутствуют: ${t.missing} · Ошибки: ${t.error}`;
    version.textContent = `${result.repository} · ${result.branch} · ${result.commit.slice(0, 12)}`;
    for (const item of result.files) {
      const row = document.createElement("div");
      row.className = "file";
      const name = document.createElement("code");
      name.textContent = item.path;
      const status = document.createElement("span");
      const label = {same:"Совпадает",modified:"Изменён",missing:"Отсутствует",error:"Ошибка"};
      status.textContent = label[item.status] || "Неизвестно";
      status.className = item.status === "same" ? "ok" : item.status === "error" ? "error" : "warn";
      const right = document.createElement("div");
      right.className = "file-actions";
      right.append(status);
      if (item.status === "modified" || item.status === "missing") {
        const inspect = document.createElement("button");
        inspect.type = "button";
        inspect.className = "diff-button";
        inspect.textContent = "Различия";
        inspect.setAttribute("aria-expanded", "false");
        const details = document.createElement("div");
        details.className = "diff-details";
        details.hidden = true;
        inspect.addEventListener("click", async () => {
          if (!details.hidden) {
            details.hidden = true;
            inspect.setAttribute("aria-expanded", "false");
            return;
          }
          details.hidden = false;
          inspect.setAttribute("aria-expanded", "true");
          details.textContent = "Загрузка различий…";
          inspect.disabled = true;
          try {
            const query = new URLSearchParams({path: item.path, commit: currentCommit});
            const response = await fetch("api/diff?" + query.toString(), {cache:"no-store"});
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || "Ошибка сравнения");
            details.replaceChildren();
            const kind = document.createElement("p");
            const names = {same:"Файлы совпадают",missing:"Файл отсутствует в HA",line_endings_only:"Различаются только окончания строк или завершающие переводы строки",content_changed:"Различается содержимое",binary_or_non_utf8:"Не удалось отобразить как текст"};
            kind.textContent = names[result.kind] || result.kind;
            details.append(kind);
            if (result.kind === "line_endings_only") {
              const info = document.createElement("p");
              info.textContent = `CRLF — HA: ${result.local_crlf}, GitHub: ${result.github_crlf}; финальный перенос — HA: ${result.local_final_newline}, GitHub: ${result.github_final_newline}`;
              details.append(info);
            }
            if (result.message) {
              const message = document.createElement("p");
              message.textContent = result.message;
              details.append(message);
            }
            if (result.diff.length) {
              const pre = document.createElement("pre");
              for (const line of result.diff) {
                const el = document.createElement("span");
                el.className = line.startsWith("+") && !line.startsWith("+++") ? "diff-add" : line.startsWith("-") && !line.startsWith("---") ? "diff-remove" : "diff-context";
                el.textContent = line + "\n";
                pre.append(el);
              }
              details.append(pre);
            }
            if (result.truncated) {
              const note = document.createElement("p");
              note.textContent = "Показана только первая часть различий (ограничение 2500 строк).";
              details.append(note);
            }
          } catch (error) {
            details.textContent = error.message || "Не удалось получить различия";
          } finally {
            inspect.disabled = false;
          }
        });
        right.append(inspect);
        row.append(name, right);
        files.append(row, details);
      } else {
        row.append(name, right);
        files.append(row);
      }
    }
    if (!result.files.length) files.textContent = "Управляемых файлов в репозитории не найдено.";
  } catch (error) {
    summary.textContent = error.message || "Ошибка";
    summary.className = "error";
  } finally {
    button.disabled = false;
  }
}
button.addEventListener("click", refresh);
refresh();
