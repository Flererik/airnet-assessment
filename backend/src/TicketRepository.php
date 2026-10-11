<?php
class TicketRepository
{
    public function all()
    {
        $rows = pg_fetch_all(query('SELECT t.id, t.title, t.status, s.name AS source_name FROM tickets t LEFT JOIN sources s ON s.id=t.source_id ORDER BY t.id')) ?: array();
        foreach ($rows as &$row) {
            $row['id'] = (int) $row['id'];
        }
        return $rows;
    }

    public function find($id)
    {
        // LEFT JOIN: заявка без источника должна загружаться с source = null.
        $row = pg_fetch_assoc(query('SELECT t.id, t.title, t.description, t.status, t.updated_at, s.id AS source_id, s.name AS source_name FROM tickets t LEFT JOIN sources s ON s.id=t.source_id WHERE t.id=$1', array($id)));
        if (!$row) {
            return null;
        }
        return array(
            'id' => (int) $row['id'],
            'title' => $row['title'],
            'description' => $row['description'],
            'status' => $row['status'],
            'source' => $row['source_id'] === null ? null : array('id' => (int) $row['source_id'], 'name' => $row['source_name']),
            'updated_at' => $row['updated_at'],
        );
    }

    public function sources()
    {
        $rows = pg_fetch_all(query('SELECT id, name FROM sources ORDER BY id')) ?: array();
        foreach ($rows as &$row) {
            $row['id'] = (int) $row['id'];
        }
        return $rows;
    }

    public function history($id)
    {
        $rows = pg_fetch_all(query('SELECT h.id, h.employee_id, e.name AS employee_name, h.old_source_id, h.new_source_id, h.created_at FROM ticket_history h JOIN employees e ON e.id=h.employee_id WHERE h.ticket_id=$1 ORDER BY h.id DESC', array($id))) ?: array();
        foreach ($rows as &$row) {
            $row['id'] = (int) $row['id'];
            $row['employee_id'] = (int) $row['employee_id'];
            $row['old_source_id'] = $row['old_source_id'] === null ? null : (int) $row['old_source_id'];
            $row['new_source_id'] = (int) $row['new_source_id'];
        }
        return $rows;
    }

    /**
     * Меняет источник заявки и пишет историю в одной транзакции.
     * Право FUNC_TICKETS_SOURCE_EDIT проверяется вызывающим кодом до этого метода.
     * Порядок проверок: заявка существует -> не архивная -> источник корректен.
     *
     * @param int $id
     * @param int|null $sourceId null, если клиент прислал некорректное значение
     * @param array $employee сотрудник из серверной сессии
     * @return array ['ticket' => ..., 'history' => ...]
     */
    public function changeSource($id, $sourceId, array $employee)
    {
        query('BEGIN');
        try {
            // FOR UPDATE сериализует параллельные изменения одной заявки,
            // поэтому old_source_id в истории всегда равен фактическому прежнему значению.
            $ticket = pg_fetch_assoc(query('SELECT status, source_id FROM tickets WHERE id=$1 FOR UPDATE', array($id)));
            if (!$ticket) {
                throw new HttpError(404, 'not_found', 'Заявка не найдена');
            }
            if ($ticket['status'] === 'archived') {
                throw new HttpError(403, 'archived', 'Архивная заявка доступна только для чтения');
            }
            if ($sourceId === null || !pg_fetch_assoc(query('SELECT id FROM sources WHERE id=$1', array($sourceId)))) {
                throw new HttpError(400, 'invalid_source', 'Выберите источник из справочника');
            }
            $oldSourceId = $ticket['source_id'] === null ? null : (int) $ticket['source_id'];
            if ($oldSourceId !== $sourceId) {
                query('UPDATE tickets SET source_id=$1, updated_at=clock_timestamp() WHERE id=$2', array($sourceId, $id));
                query('INSERT INTO ticket_history(ticket_id, employee_id, old_source_id, new_source_id) VALUES($1,$2,$3,$4)', array($id, $employee['id'], $oldSourceId, $sourceId));
            }
            $result = array('ticket' => $this->find($id), 'history' => $this->history($id));
            query('COMMIT');
            return $result;
        } catch (Throwable $error) {
            @pg_query_params(db(), 'ROLLBACK', array());
            throw $error;
        }
    }
}
