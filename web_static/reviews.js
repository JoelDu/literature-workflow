'use strict';
const message = document.getElementById('message');
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  });
  const result = await response.json();
  if (!response.ok || !result.success) throw new Error(result.error || '请求失败');
  return result.data;
}
const labels = {running: '生成中', done: '已完成', partial: '部分完成', failed: '失败', interrupted: '已中断'};
async function refresh() {
  try {
    const jobs = await api('/api/review/jobs');
    const container = document.getElementById('jobs');
    container.replaceChildren();
    for (const job of jobs) {
      const card = document.createElement('article');
      const title = document.createElement('h3');
      title.textContent = `${job.topic} — ${labels[job.status] || job.status} (${job.progress || 0}%)`;
      card.append(title);
      if (['partial', 'failed', 'interrupted'].includes(job.status)) {
        const button = document.createElement('button');
        button.textContent = '从检查点继续';
        button.onclick = async () => {
          button.disabled = true;
          try { await api(`/api/review/jobs/${encodeURIComponent(job.job_id)}/resume`, {}); await refresh(); }
          catch (error) { message.textContent = error.message; button.disabled = false; }
        };
        card.append(button);
      }
      for (const kind of ['md', 'docx', 'manifest']) {
        if (!job[`filename_${kind}`]) continue;
        const link = document.createElement('a');
        link.textContent = `下载 ${kind}`;
        link.href = `/api/review/download?filename=${encodeURIComponent(job[`filename_${kind}`])}`;
        card.append(link);
      }
      const logs = document.createElement('pre');
      logs.textContent = (job.log || []).slice(-12).join('\n');
      card.append(logs); container.append(card);
    }
  } catch (error) { message.textContent = error.message; }
}
document.getElementById('create').onsubmit = async event => {
  event.preventDefault();
  const button = event.target.querySelector('button'); button.disabled = true;
  const data = Object.fromEntries(new FormData(event.target));
  data.words = Number(data.words); data.sections = Number(data.sections);
  try { await api('/api/review/generate', data); message.textContent = ''; await refresh(); }
  catch (error) { message.textContent = error.message; }
  finally { button.disabled = false; }
};
refresh(); setInterval(refresh, 5000);
