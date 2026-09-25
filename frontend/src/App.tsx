import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { TaskList } from "./pages/TaskList";
import { TaskDetail } from "./pages/TaskDetail";
import { Evaluation } from "./pages/Evaluation";

function App() {
  return (
    <div className="app-shell">
      <header className="app-topbar">
        <div className="app-title">
          Research Assurance Console
          <small>Auditable Financial Research Agent</small>
        </div>
        <nav className="app-nav">
          <NavLink to="/tasks" className={({ isActive }) => (isActive ? "active" : "")}>
            Tasks
          </NavLink>
          <NavLink to="/evaluation" className={({ isActive }) => (isActive ? "active" : "")}>
            Evaluation
          </NavLink>
        </nav>
      </header>
      <main className="app-main">
        <Routes>
          <Route path="/" element={<Navigate to="/tasks" replace />} />
          <Route path="/tasks" element={<TaskList />} />
          <Route path="/tasks/:taskId" element={<TaskDetail />} />
          <Route path="/evaluation" element={<Evaluation />} />
        </Routes>
      </main>
    </div>
  );
}

export default App;
