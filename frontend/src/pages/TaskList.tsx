import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, listTasks } from "../api/client";
import type { TaskSummary } from "../api/types";
import { StatusBadge } from "../components/StatusBadge";

export function TaskList() {
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    listTasks()
      .then((res) => setTasks(res.tasks))
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load tasks"));
  }, []);

  if (error) return <p className="error-text">{error}</p>;
  if (tasks === null) return <p className="loading">Loading tasks…</p>;

  return (
    <div className="panel">
      <div className="panel-title">
        <span>Research Tasks ({tasks.length})</span>
      </div>
      {tasks.length === 0 ? (
        <p className="panel-empty">
          No tasks yet. Run <code>python3 scripts/seed_demo_data.py</code> against the backend to
          populate demo data.
        </p>
      ) : (
        <table className="dense">
          <thead>
            <tr>
              <th>Question</th>
              <th>State</th>
              <th>Created by</th>
              <th>Classification</th>
              <th>Version</th>
              <th>Publishable now</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((task) => (
              <tr
                key={task.task_id}
                className="clickable"
                onClick={() => navigate(`/tasks/${task.task_id}`)}
              >
                <td>{task.question_text}</td>
                <td>
                  <StatusBadge state={task.state} />
                </td>
                <td className="mono">{task.created_by}</td>
                <td>
                  <span className="pill">{task.classification}</span>
                </td>
                <td>{task.claim_set_version}</td>
                <td>{task.publication_allowed ? "Yes" : "No"}</td>
                <td className="faint">
                  {task.created_at ? new Date(task.created_at).toLocaleString() : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
