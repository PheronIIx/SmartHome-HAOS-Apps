"use strict";
const button = document.getElementById("refresh");
const summary = document.getElementById("summary");
const version = document.getElementById("version");
const files = document.getElementById("files");
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
      row.append(name, status);
      files.append(row);
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
