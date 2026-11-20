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
    summary.textContent = `Совпадают: ${t.same} · Только формат строк: ${t.format_only} · Изменено содержимое: ${t.modified} · Не определено: ${t.unclassified} · Отсутствуют: ${t.missing} · Ошибки: ${t.error}`;
    version.textContent = `${result.repository} · ${result.branch} · ${result.commit.slice(0, 12)}`;
    for (const item of result.files) {
      const row = document.createElement("div");
      row.className = "file";
      const name = document.createElement("code");
      name.textContent = item.path;
      const status = document.createElement("span");
      const label = {same:"Совпадает",format_only:"Только формат строк",modified:"Изменён",unclassified:"Не определено",missing:"Отсутствует",error:"Ошибка"};
      status.textContent = label[item.status] || "Неизвестно";
      status.className = ["same", "format_only"].includes(item.status) ? "ok" : item.status === "error" ? "error" : "warn";
      const right = document.createElement("div");
      right.className = "file-actions";
      right.append(status);
      if (["modified", "missing", "format_only", "unclassified"].includes(item.status)) {
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

const baselineInit = document.getElementById("baseline-init");
const syncCheck = document.getElementById("sync-check");
const syncStatus = document.getElementById("sync-status");
const syncFiles = document.getElementById("sync-files");

function appendDiffSection(container, heading, result) {
  const section = document.createElement("section");
  const title = document.createElement("h4");
  title.textContent = heading + " · " + (result.kind || "неизвестно");
  section.append(title);
  if (result.diff && result.diff.length) {
    const pre = document.createElement("pre");
    for (const line of result.diff) {
      const el = document.createElement("span");
      el.className = line.startsWith("+") && !line.startsWith("+++") ? "diff-add"
        : line.startsWith("-") && !line.startsWith("---") ? "diff-remove" : "diff-context";
      el.textContent = line + "\n";
      pre.append(el);
    }
    section.append(pre);
  }
  if (result.truncated) {
    const note = document.createElement("p");
    note.textContent = "Diff ограничен первыми 2500 строками.";
    section.append(note);
  }
  container.append(section);
}

async function loadSyncPlan() {
  syncCheck.disabled = true;
  syncStatus.className = "";
  syncStatus.textContent = "Сверяем манифест с GitHub main…";
  syncFiles.replaceChildren();
  try {
    const response = await fetch("api/sync/plan", {cache:"no-store"});
    const result = await readBackupJson(response);
    if (!response.ok) throw new Error(result.error || "Ошибка плана синхронизации");
    if (!result.configured) { syncStatus.textContent = result.message || "Сначала инициализируйте манифест."; return; }
    const t = result.totals;
    syncStatus.textContent = "Установленная база: " + result.baseline_files + " файлов · GitHub: " + result.target.slice(0,12)
      + " · обновления: " + (t.candidate_review + t.new_file_review + t.new_file_conflict + t.deletion_review)
      + " · только форматирование: " + t.format_only
      + " · локальные конфликты: " + (t.blocked_local_drift + t.new_file_conflict)
      + " · не удалось проверить: " + t.blocked_unverified + " · установка отключена";
    const labels = {no_change:"Без изменений",format_only:"Только форматирование · не требует обновления",
      candidate_review:"Обновление",blocked_local_drift:"Конфликт локальной версии",
      blocked_unverified:"Не удалось проверить · установка заблокирована",
      new_file_review:"Новый файл (HA отсутствует)",new_file_present:"Новый путь уже совпадает с GitHub",
      new_file_conflict:"Новый путь: локальный файл требует проверки",deletion_review:"Файл удалён из GitHub"};
    for (const item of result.files) {
      const row=document.createElement("div"); row.className="file";
      const name=document.createElement("code"); name.textContent=item.path;
      const status=document.createElement("span"); status.textContent=(labels[item.action]||item.action)+" · "+item.local;
      status.className=["no_change","format_only","new_file_present"].includes(item.action)?"ok":"warn";
      const actions=document.createElement("div"); actions.className="file-actions"; actions.append(status);
      if(!["no_change","format_only"].includes(item.action)){
        const button=document.createElement("button"); button.type="button"; button.className="diff-button"; button.textContent="Diff";
        const details=document.createElement("div"); details.className="diff-details"; details.hidden=true;
        button.addEventListener("click",async()=>{
          if(!details.hidden){details.hidden=true;button.setAttribute("aria-expanded","false");return;}
          details.hidden=false;button.setAttribute("aria-expanded","true");details.textContent="Загружаем diff…";button.disabled=true;
          try{
            const query=new URLSearchParams({path:item.path,commit:result.target});
            const response=await fetch("api/sync/diff?"+query.toString(),{cache:"no-store"});
            const data=await readBackupJson(response); if(!response.ok)throw new Error(data.error||"Не удалось получить diff");
            details.replaceChildren();
            if(data.baseline)appendDiffSection(details,"HA ↔ установленная база",data.baseline);
            if(data.target)appendDiffSection(details,"HA ↔ GitHub main",data.target);
          }catch(error){details.textContent=error.message||"Ошибка diff";}finally{button.disabled=false;}
        });
        actions.append(button); row.append(name,actions); syncFiles.append(row,details);
      }else{row.append(name,actions);syncFiles.append(row);}
    }
    if(!result.files.length)syncFiles.textContent="Изменений в allowlist нет.";
  }catch(error){syncStatus.textContent=error.message||"Ошибка проверки";syncStatus.className="error";}
  finally{syncCheck.disabled=false;}
}

baselineInit.addEventListener("click",async()=>{
  if(!window.confirm("Создать /data/installed-manifest.json из утверждённого baseline_commit? Файлы Home Assistant не будут изменены. Для новых путей будет принят только файл, совпадающий с GitHub."))return;
  baselineInit.disabled=true;syncStatus.className="";syncStatus.textContent="Проверяем базовый коммит и локальные файлы…";
  try{
    const result=await backupRequest("api/baseline/initialize",{confirmation:"INITIALIZE MANIFEST"});
    syncStatus.textContent="Манифест создан: "+result.file_count+" файлов. Новые пути приняты: "+result.adopted_new_paths.length
      +"; локальные расхождения новых путей: "+result.untracked_local_paths.length+"; отсутствуют: "+result.untracked_missing_paths.length+". HA не изменялся.";
    await loadSyncPlan();
  }catch(error){syncStatus.textContent=error.message||"Не удалось создать манифест";syncStatus.className="error";}
  finally{baselineInit.disabled=false;}
});
syncCheck.addEventListener("click",loadSyncPlan);
const backupCreate = document.getElementById("backup-create");
const backupRefresh = document.getElementById("backup-refresh");
const backupStatus = document.getElementById("backup-status");
const backupList = document.getElementById("backup-list");
let backupCsrf = "";

async function readBackupJson(response) {
  const type = response.headers.get("Content-Type") || "";
  if (!type.toLowerCase().includes("application/json")) {
    throw new Error("Сервер вернул HTTP " + response.status + " вместо JSON. Проверьте доступ через Ingress и журнал SmartHome Deploy.");
  }
  return response.json();
}

async function backupRequest(path, body) {
  if (!backupCsrf) {
    const auth = await fetch("api/security", {cache:"no-store"});
    if (!auth.ok) throw new Error("Не удалось получить подтверждение доступа");
    const data = await readBackupJson(auth);
    backupCsrf = data.csrf_token;
  }
  const response = await fetch(path, {
    method:"POST",
    headers:{"Content-Type":"application/json", "X-CSRF-Token":backupCsrf},
    body:JSON.stringify(body),
    cache:"no-store"
  });
  const result = await readBackupJson(response);
  if (!response.ok) throw new Error(result.error || "Ошибка резервного копирования");
  return result;
}

async function loadBackups() {
  backupRefresh.disabled = true;
  backupStatus.textContent = "Загружаем список…";
  backupList.replaceChildren();
  try {
    const response = await fetch("api/backups", {cache:"no-store"});
    const result = await readBackupJson(response);
    if (!response.ok) throw new Error(result.error || "Не удалось получить список");
    backupStatus.textContent = result.backups.length
      ? "Архивов: " + result.backups.length + (result.truncated ? " · показаны последние 100" : "")
      : "Резервных копий пока нет.";
    for (const item of result.backups) {
      const row = document.createElement("div");
      row.className = "file";
      const info = document.createElement("div");
      const name = document.createElement("code");
      name.textContent = item.backup;
      const detail = document.createElement("div");
      detail.textContent = item.created_at + " · " + Math.round(item.archive_bytes / 1024) + " КиБ";
      info.append(name, detail);
      const actions = document.createElement("div");
      actions.className = "file-actions";
      for (const [label, endpoint, verb] of [
        ["Проверить", "api/backups/verify", "Проверка"],
        ["Проверить восстановление", "api/backups/rehearse", "Тестовое восстановление"]
      ]) {
        const action = document.createElement("button");
        action.type = "button";
        action.className = "diff-button";
        action.textContent = label;
        action.addEventListener("click", async () => {
          action.disabled = true;
          backupStatus.textContent = verb + "…";
          try {
            const result = await backupRequest(endpoint, {backup:item.backup});
            backupStatus.textContent = verb + " выполнено: " + result.file_count + " файлов · целостность подтверждена. Конфигурация HA не изменена.";
          } catch (error) {
            backupStatus.textContent = error.message || "Ошибка";
            backupStatus.className = "error";
          } finally {
            action.disabled = false;
          }
        });
        actions.append(action);
      }
      row.append(info, actions);
      backupList.append(row);
    }
  } catch (error) {
    backupStatus.textContent = error.message || "Ошибка";
    backupStatus.className = "error";
  } finally {
    backupRefresh.disabled = false;
  }
}
backupCreate.addEventListener("click", async () => {
  if (!window.confirm("Сохранить копию всех управляемых файлов в /data приложения? Конфигурация Home Assistant не изменится.")) return;
  backupCreate.disabled = true;
  backupStatus.className = "";
  backupStatus.textContent = "Создаём и проверяем архив…";
  try {
    const result = await backupRequest("api/backups/create", {});
    await loadBackups();
    const skipped = Array.isArray(result.skipped_uninstalled) && result.skipped_uninstalled.length
      ? " · Не включены (ещё не установлены): " + result.skipped_uninstalled.join(", ")
      : "";
    backupStatus.textContent = "Создана и проверена копия " + result.backup + " · " + result.file_count + " файлов." + skipped;
  } catch (error) {
    backupStatus.textContent = error.message || "Не удалось создать копию";
    backupStatus.className = "error";
  } finally {
    backupCreate.disabled = false;
  }
});
backupRefresh.addEventListener("click", loadBackups);
loadBackups();

const deployPrepare = document.getElementById("deploy-prepare");
const deployStatus = document.getElementById("deploy-status");
const deployPreview = document.getElementById("deploy-preview");
const deployRefresh = document.getElementById("deploy-refresh");
const deployHistory = document.getElementById("deploy-history");

async function loadDeployHistory() {
  deployRefresh.disabled = true;
  deployHistory.replaceChildren();
  try {
    const response = await fetch("api/deploy/history", {cache:"no-store"});
    const data = await readBackupJson(response);
    if (!response.ok) throw new Error(data.error || "Ошибка истории");
    if (!data.operations.length) deployHistory.textContent = "Установок пока нет.";
    for (const item of data.operations) {
      const row = document.createElement("div");
      row.className = "file";
      const info = document.createElement("div");
      const name = document.createElement("code");
      name.textContent = item.id;
      const detail = document.createElement("div");
      detail.textContent = item.created_at + " · " + item.status + " · " + item.path;
      info.append(name, detail);
      row.append(info);
      if (item.status === "installed") {
        const undo = document.createElement("button");
        undo.textContent = "Откатить";
        undo.type = "button";
        undo.addEventListener("click", async () => {
          if (!window.confirm("Вернуть исходный house-card.js из архива? Перед откатом будет создана копия установленной версии.")) return;
          undo.disabled = true;
          deployStatus.textContent = "Проверяем архив и откатываем…";
          try {
            const result = await backupRequest("api/deploy/rollback", {
              operation_id:item.id, confirmation:"ROLLBACK house-card.js"
            });
            deployStatus.textContent = "Откат: " + result.status + " · " + result.id;
            await loadDeployHistory();
            await loadBackups();
          } catch (error) {
            deployStatus.textContent = error.message || "Ошибка отката";
            await loadDeployHistory();
          } finally {
            undo.disabled = false;
          }
        });
        row.append(undo);
      }
      deployHistory.append(row);
    }
  } catch (error) {
    deployHistory.textContent = error.message || "История недоступна";
  } finally {
    deployRefresh.disabled = false;
  }
}
deployPrepare.addEventListener("click", async () => {
  deployPrepare.disabled = true;
  deployPreview.replaceChildren();
  deployStatus.textContent = "Подготавливаем план с проверкой исходного файла…";
  try {
    const plan = await backupRequest("api/deploy/prepare", {});
    deployStatus.textContent = "План действует 5 минут. Сверьте контрольные суммы.";
    const description = document.createElement("p");
    description.textContent = plan.path + " · GitHub " + plan.target_commit.slice(0,12);
    const before = document.createElement("code");
    before.textContent = "HA SHA-256: " + plan.local_sha256;
    const after = document.createElement("p");
    after.textContent = "Цель SHA-256: " + plan.target_sha256;
    const install = document.createElement("button");
    install.type = "button";
    install.textContent = "Установить тестовый комментарий";
    install.addEventListener("click", async () => {
      if (!window.confirm("Подтвердить запись в house-card.js? Автоматический архив будет создан непосредственно перед заменой.")) return;
      install.disabled = true;
      deployStatus.textContent = "Создаём архив, проверяем и устанавливаем…";
      try {
        const result = await backupRequest("api/deploy/install", {
          plan_id:plan.plan_id, confirmation:"INSTALL house-card.js"
        });
        deployStatus.textContent = "Установлено: " + result.id + " · архив " + result.backup;
        deployPreview.replaceChildren();
        await loadDeployHistory();
        await loadBackups();
      } catch (error) {
        deployStatus.textContent = error.message || "Установка отклонена";
        await loadDeployHistory();
      } finally {
        install.disabled = false;
      }
    });
    deployPreview.append(description, before, after, install);
  } catch (error) {
    deployStatus.textContent = error.message || "Подготовка заблокирована";
  } finally {
    deployPrepare.disabled = false;
  }
});
deployRefresh.addEventListener("click", loadDeployHistory);
loadDeployHistory();
