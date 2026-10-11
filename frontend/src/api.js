let csrf = '';

export function setCsrf(value) {
  csrf = value;
}

export async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    credentials: 'same-origin',
    ...options,
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, ...options.headers },
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    // Не-JSON ответ (например, ошибка прокси) превращаем в понятную ошибку ниже.
  }
  if (!response.ok || data === null) {
    const error = new Error(data?.message || `Ошибка сервера (HTTP ${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}
