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
      + " · не удалось проверить: " + t.blocked_unverified + " · JS-установка доступна только при явном включении";
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
syncCheck.addEventListener("click", () => { batchPreview.replaceChildren(); batchMessage(""); loadSyncPlan(); });
loadSyncPlan();
const preflightCheck = document.getElementById("preflight-check");
const preflightStatus = document.getElementById("preflight-status");
const preflightFiles = document.getElementById("preflight-files");

preflightCheck.addEventListener("click", async () => {
  preflightCheck.disabled = true;
  preflightStatus.className = "";
  preflightStatus.textContent = "Проверяем файлы, SHA-256 и ограничения безопасности…";
  preflightFiles.replaceChildren();
  try {
    const response = await fetch("api/sync/preflight", {cache:"no-store"});
    const data = await readBackupJson(response);
    if (!response.ok) throw new Error(data.error || "Не удалось подготовить предварительный план");
    if (!data.configured) {
      preflightStatus.textContent = data.message || "Сначала инициализируйте манифест.";
      return;
    }
    const c = data.counts;
    preflightStatus.textContent = "GitHub " + data.target_commit.slice(0, 12)
      + " · обновить: " + c.update + " · добавить: " + c.add
      + " · удаления на проверку: " + c.delete_review
      + " · блокировки: " + c.blocked
      + " · пропустить: " + c.skip
      + " · YAML-проверка: " + (data.requires_yaml_validation ? "необходима" : "не требуется")
      + " · полный бэкап HAOS: " + (data.requires_fresh_full_haos_backup ? "необходим" : "не требуется")
      + " · установка отключена";
    const summary = document.createElement("p");
    const fingerprint = data.plan_fingerprint;
    summary.textContent = "Отпечаток плана: " + fingerprint.slice(0, 20)
      + "… · Это диагностический отпечаток, а не разрешение на установку.";
    preflightFiles.append(summary);
    const names = {
      update:"Кандидат на обновление", add:"Новый файл", delete_review:"Удаление — отдельное согласование",
      blocked:"Заблокирован", skip:"Не требуется"
    };
    for (const item of data.files) {
      if (item.disposition === "skip") continue;
      const row = document.createElement("div");
      row.className = "file";
      const info = document.createElement("div");
      const path = document.createElement("code");
      path.textContent = item.path;
      const description = document.createElement("div");
      description.textContent = (names[item.disposition] || item.disposition)
        + " · " + item.risk + " · " + item.source_action
        + (item.snapshot_error ? " · локальная проверка: " + item.snapshot_error : "");
      const hashes = document.createElement("small");
      hashes.textContent = "HA SHA-256: " + (item.local?.sha256 || "отсутствует/не проверен")
        + " · GitHub SHA-256: " + (item.target_sha256 || "не применимо");
      info.append(path, description, hashes);
      row.append(info);
      preflightFiles.append(row);
    }
    if (!data.files.some(item => item.disposition !== "skip")) {
      const empty = document.createElement("p");
      empty.textContent = "Файлов, требующих действий, нет.";
      preflightFiles.append(empty);
    }
  } catch (error) {
    preflightStatus.textContent = error.message || "Предустановочная проверка не выполнена";
    preflightStatus.className = "error";
  } finally {
    preflightCheck.disabled = false;
  }
});

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

// 1.0.0: opt-in JS-only batch operations via the HA Ingress interface.
const batchPrepare = document.getElementById("batch-prepare");
const batchRefresh = document.getElementById("batch-refresh");
const batchStatus = document.getElementById("batch-status");
const batchPreview = document.getElementById("batch-preview");
const batchHistory = document.getElementById("batch-history");

function batchMessage(message, isError = false) {
  batchStatus.textContent = message;
  batchStatus.className = isError ? "error" : "";
}

async function loadBatchHistory() {
  batchRefresh.disabled = true;
  batchHistory.replaceChildren();
  try {
    const response = await fetch("api/multifile/history", {cache:"no-store"});
    const result = await readBackupJson(response);
    if (!response.ok) throw new Error(result.error || "Не удалось прочитать журнал");
    const operations = result.operations || [];
    if (!operations.length) { batchHistory.textContent = "JS-установок пока нет."; return; }
    for (const op of operations) {
      const row = document.createElement("div");
      row.className = "file";
      const info = document.createElement("div");
      const heading = document.createElement("code");
      heading.textContent = op.id + " · " + op.status;
      const detail = document.createElement("div");
      detail.textContent = op.paths.join(", ") + " · GitHub " + op.target_commit.slice(0, 12);
      info.append(heading, detail);
      row.append(info);
      if (["installed", "installing", "needs_attention"].includes(op.status)) {
        const restore = document.createElement("button");
        restore.type = "button";
        restore.textContent = "Откат";
        restore.addEventListener("click", async () => {
          if (!window.confirm("Восстановить исходные JS-файлы для операции " + op.id + "? Текущие изменения могут блокировать откат.")) return;
          restore.disabled = true;
          try {
            const out = await backupRequest("api/multifile/rollback", {operation_id:op.id, confirmation:"ROLLBACK JS BATCH"});
            batchMessage("Откат: " + out.status + " · " + out.id);
            await loadBatchHistory();
          } catch (error) { batchMessage(error.message || "Ошибка отката", true); }
          finally { restore.disabled = false; }
        });
        row.append(restore);
      }
      batchHistory.append(row);
    }
  } catch (error) { batchMessage(error.message || "Ошибка истории", true); }
  finally { batchRefresh.disabled = false; }
}

batchPrepare.addEventListener("click", async () => {
  batchPrepare.disabled = true;
  batchPreview.replaceChildren();
  batchMessage("Проверяем GitHub, локальные файлы и манифест…");
  try {
    const plan = await backupRequest("api/multifile/prepare", {});
    batchMessage("Готово к проверке: " + plan.files.length + " JS · коммит " + plan.target_commit.slice(0, 12) + " · план действует 5 минут.");
    const list = document.createElement("div");
    for (const item of plan.files) {
      const row = document.createElement("div");
      row.className = "file";
      const path = document.createElement("code");
      path.textContent = item.path;
      const info = document.createElement("small");
      info.textContent = item.action === "add"
        ? "Новый файл · GitHub SHA-256: " + item.after_sha256.slice(0, 12) + "…"
        : "Обновление · SHA-256 HA: " + item.before_sha256.slice(0, 12) + "… → GitHub: " + item.after_sha256.slice(0, 12) + "…";
      row.append(path, info);
      list.append(row);
    }
    const install = document.createElement("button");
    install.type = "button";
    install.textContent = "Установить показанные JS-файлы";
    install.addEventListener("click", async () => {
      if (!window.confirm("Установить ровно " + plan.files.length + " JS из коммита " + plan.target_commit.slice(0, 12) + "? Перед записью будет создана и проверена резервная копия.")) return;
      install.disabled = true;
      batchMessage("Проверяем план и выполняем транзакционную установку…");
      try {
        const out = await backupRequest("api/multifile/install", {plan_id:plan.plan_id, confirmation:"INSTALL JS BATCH"});
        batchMessage("Установлено: " + out.paths.join(", ") + " · операция " + out.id);
        batchPreview.replaceChildren();
        await loadBatchHistory();
      } catch (error) {
        batchMessage((error.message || "Установка не выполнена") + ". Проверьте историю и журнал приложения.", true);
        await loadBatchHistory();
      } finally { install.disabled = false; }
    });
    batchPreview.append(list, install);
  } catch (error) { batchMessage(error.message || "JS-обновление недоступно", true); }
  finally { batchPrepare.disabled = false; }
});
batchRefresh.addEventListener("click", loadBatchHistory);
loadBatchHistory();
