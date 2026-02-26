const btn = document.getElementById('btn');
const fileInput = document.getElementById('file');
const statusEl = document.getElementById('status');

function setStatus(msg) {
  statusEl.textContent = msg;
}

btn.addEventListener('click', async () => {
  const file = fileInput.files && fileInput.files[0];
  if (!file) {
    setStatus('Please choose a file.');
    return;
  }

  btn.disabled = true;
  setStatus('Running… Please wait.');

  try {
    const fd = new FormData();
    fd.append('file', file);

    const res = await fetch('/run', {
      method: 'POST',
      body: fd,
    });

    if (!res.ok) {
      setStatus('Failed. Please try again.');
      btn.disabled = false;
      return;
    }

    const blob = await res.blob();
    const cd = res.headers.get('content-disposition') || '';
    const match = cd.match(/filename="?([^";]+)"?/i);
    const filename = match ? match[1] : 'nmc.pdf';

    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);

    setStatus('Downloaded.');
  } catch (e) {
    setStatus('Failed. Please try again.');
  } finally {
    btn.disabled = false;
  }
});
