// @vitest-environment jsdom
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from './App.jsx';

const editor = { id: 2, name: 'Илья Учебный', role: 'editor', permissions: ['FUNC_TICKETS_SOURCE_EDIT', 'FUNC_TICKETS_VIEW'] };
const viewer = { id: 1, name: 'Анна Примерова', role: 'viewer', permissions: ['FUNC_TICKETS_VIEW'] };
const sources = [{ id: 1, name: 'Телефон' }, { id: 2, name: 'Личный кабинет' }, { id: 3, name: 'Сотрудник' }];
const list = [
  { id: 101, title: 'Не работает интернет', status: 'active', source_name: 'Телефон' },
  { id: 102, title: 'Настроить роутер', status: 'incoming', source_name: 'Личный кабинет' },
  { id: 103, title: 'Нет изображения с камеры', status: 'active', source_name: null },
  { id: 104, title: 'Заменить блок питания', status: 'archived', source_name: 'Сотрудник' },
];
const card = (id, sourceId, status = 'active') => ({
  ticket: {
    id, title: list.find(t => t.id === id).title, description: `Описание ${id}`, status,
    source: sourceId ? sources.find(s => s.id === sourceId) : null, updated_at: '2026-01-01 10:00:00+03',
  },
  history: [],
});

// Фейковый сервер: каждый запрос ждёт, пока тест явно не ответит на него.
// Так порядок ответов полностью под контролем теста, независимо от задержек.
let pending;
function json(status, data) {
  return { ok: status < 400, status, json: async () => data, text: async () => JSON.stringify(data) };
}
beforeEach(() => {
  pending = [];
  vi.stubGlobal('fetch', vi.fn((url, options = {}) => new Promise((resolve, reject) => {
    const request = { url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : undefined, resolve, reject };
    options.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    pending.push(request);
  })));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function take(url, method = 'GET') {
  const index = pending.findIndex(r => r.url === `/api${url}` && r.method === method);
  if (index < 0) throw new Error(`Нет запроса ${method} ${url}; ожидают: ${pending.map(r => `${r.method} ${r.url}`).join(', ')}`);
  return pending.splice(index, 1)[0];
}
async function reply(url, status, data, method = 'GET') {
  const request = take(url, method);
  await act(async () => { request.resolve(json(status, data)); });
  return request;
}
async function boot(employee = editor) {
  const user = userEvent.setup();
  render(<App />);
  await reply('/me', 200, { employee, csrf: 't' });
  await reply('/tickets', 200, { tickets: list });
  await reply('/sources', 200, { sources });
  return user;
}
const row = id => screen.getByRole('button', { name: new RegExp(`#${id}`) });
const panel = () => screen.getByRole('region', { name: 'Карточка заявки' });
const listRegion = () => screen.getByRole('region', { name: 'Список заявок' });

describe('Карточка заявки', () => {
  it('поздний ответ предыдущей заявки не подменяет выбранную (исходная ошибка 1)', async () => {
    const user = await boot();
    await user.click(row(101));
    await user.click(row(102));
    await reply('/tickets/102', 200, card(102, 2));
    // Медленный ответ по 101 приходит последним.
    const slow = pending.find(r => r.url === '/api/tickets/101');
    if (slow) await act(async () => { slow.resolve(json(200, card(101, 1))); });
    expect(within(panel()).getByRole('heading', { name: 'Настроить роутер' })).toBeTruthy();
    expect(within(panel()).queryByText('Не работает интернет')).toBeNull();
    expect(within(panel()).getByRole('combobox').value).toBe('2');
  });

  it('пока карточка загружается, данные предыдущей не показываются', async () => {
    const user = await boot();
    await user.click(row(102));
    await reply('/tickets/102', 200, card(102, 2));
    await user.click(row(101));
    expect(within(panel()).getByRole('status').textContent).toMatch(/Загрузка/);
    expect(within(panel()).queryByText('Настроить роутер')).toBeNull();
  });

  it('заявка без источника показывает «Не указан» (исходная ошибка 2)', async () => {
    const user = await boot(viewer);
    await user.click(row(103));
    await reply('/tickets/103', 200, card(103, null));
    expect(within(panel()).getByText('Не указан')).toBeTruthy();
    expect(within(listRegion()).getByText('Не указан')).toBeTruthy();
  });

  it('ошибка загрузки карточки показывается', async () => {
    const user = await boot();
    await user.click(row(101));
    await reply('/tickets/101', 500, { error: 'internal_error', message: 'Не удалось обработать запрос' });
    expect(within(panel()).getByRole('alert').textContent).toBe('Не удалось обработать запрос');
  });

  it('просмотрщик и архивная заявка — только чтение', async () => {
    let user = await boot(viewer);
    await user.click(row(102));
    await reply('/tickets/102', 200, card(102, 2));
    expect(within(panel()).queryByRole('combobox')).toBeNull();
    expect(within(panel()).getByText('Личный кабинет')).toBeTruthy();
    cleanup();
    user = await boot(editor);
    await user.click(row(104));
    await reply('/tickets/104', 200, card(104, 3, 'archived'));
    expect(within(panel()).queryByRole('combobox')).toBeNull();
    expect(within(panel()).getByText(/только для чтения/)).toBeTruthy();
  });
});

describe('Сохранение источника', () => {
  async function openEditable(id = 102, sourceId = 2) {
    const user = await boot();
    await user.click(row(id));
    await reply(`/tickets/${id}`, 200, card(id, sourceId));
    return user;
  }
  const saved = (id, oldId, newId) => ({
    ticket: card(id, newId).ticket,
    history: [{ id: 7, employee_id: 2, employee_name: 'Илья Учебный', old_source_id: oldId, new_source_id: newId, created_at: '2026-01-01 10:05:00+03' }],
  });

  it('успешное сохранение обновляет карточку, историю и список', async () => {
    const user = await openEditable();
    await user.selectOptions(within(panel()).getByRole('combobox'), '3');
    await user.click(within(panel()).getByRole('button', { name: 'Сохранить' }));
    const request = await reply('/tickets/102/source', 200, saved(102, 2, 3), 'POST');
    expect(request.body).toEqual({ source_id: 3 });
    expect(within(panel()).getByText('Источник сохранён')).toBeTruthy();
    expect(within(panel()).getByText('Личный кабинет → Сотрудник')).toBeTruthy();
    expect(within(row(102)).getByText('Сотрудник')).toBeTruthy();
  });

  it('повторная отправка во время сохранения невозможна', async () => {
    const user = await openEditable();
    await user.selectOptions(within(panel()).getByRole('combobox'), '3');
    const button = within(panel()).getByRole('button', { name: 'Сохранить' });
    await user.click(button);
    await user.click(button);
    await user.dblClick(button);
    expect(pending.filter(r => r.method === 'POST')).toHaveLength(1);
    expect(within(panel()).getByRole('button', { name: 'Сохранение…' }).disabled).toBe(true);
    await reply('/tickets/102/source', 200, saved(102, 2, 3), 'POST');
    expect(within(panel()).getByRole('button', { name: 'Сохранить' }).disabled).toBe(false);
  });

  it('ошибка сохранения показывается, карточка остаётся', async () => {
    const user = await openEditable();
    await user.selectOptions(within(panel()).getByRole('combobox'), '3');
    await user.click(within(panel()).getByRole('button', { name: 'Сохранить' }));
    await reply('/tickets/102/source', 403, { error: 'forbidden', message: 'Недостаточно прав' }, 'POST');
    expect(within(panel()).getByRole('alert').textContent).toBe('Недостаточно прав');
    expect(within(panel()).getByRole('heading', { name: 'Настроить роутер' })).toBeTruthy();
    expect(within(row(102)).getByText('Личный кабинет')).toBeTruthy();
  });

  it('ответ сохранения предыдущей заявки не подменяет текущую карточку', async () => {
    const user = await openEditable();
    await user.selectOptions(within(panel()).getByRole('combobox'), '3');
    await user.click(within(panel()).getByRole('button', { name: 'Сохранить' }));
    await user.click(row(101));
    await reply('/tickets/101', 200, card(101, 1));
    await reply('/tickets/102/source', 200, saved(102, 2, 3), 'POST');
    expect(within(panel()).getByRole('heading', { name: 'Не работает интернет' })).toBeTruthy();
    expect(within(panel()).getByRole('combobox').value).toBe('1');
    expect(within(panel()).queryByText('Источник сохранён')).toBeNull();
    // Список при этом отражает успешное изменение 102.
    expect(within(row(102)).getByText('Сотрудник')).toBeTruthy();
  });

  it('ошибка сохранения предыдущей заявки не появляется в текущей карточке', async () => {
    const user = await openEditable();
    await user.selectOptions(within(panel()).getByRole('combobox'), '3');
    await user.click(within(panel()).getByRole('button', { name: 'Сохранить' }));
    await user.click(row(101));
    await reply('/tickets/101', 200, card(101, 1));
    await reply('/tickets/102/source', 500, { error: 'internal_error', message: 'Сбой' }, 'POST');
    expect(within(panel()).queryByRole('alert')).toBeNull();
  });

  it('устаревшая загрузка той же заявки не затирает результат сохранения', async () => {
    const user = await openEditable();
    await user.selectOptions(within(panel()).getByRole('combobox'), '3');
    await user.click(within(panel()).getByRole('button', { name: 'Сохранить' }));
    await user.click(row(101));
    await user.click(row(102));            // повторная загрузка 102 стартует до ответа сохранения
    await reply('/tickets/102/source', 200, saved(102, 2, 3), 'POST');
    const stale = pending.find(r => r.url === '/api/tickets/102');
    await act(async () => { stale.resolve(json(200, card(102, 2))); });   // данные до сохранения
    expect(within(panel()).getByRole('combobox').value).toBe('3');
    expect(within(panel()).getByText('Личный кабинет → Сотрудник')).toBeTruthy();
  });
});
