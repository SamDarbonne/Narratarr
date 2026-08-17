import { NavLink } from "react-router-dom";

const LINKS = [
  { to: "/jobs", label: "Library", icon: "📚" },
  { to: "/gates", label: "Review queue", icon: "🛎️" },
  { to: "/targets", label: "Targets", icon: "🎯" },
  { to: "/settings", label: "Settings", icon: "⚙️" },
  { to: "/system", label: "System status", icon: "🖥️" },
];

export function Sidebar() {
  return (
    <nav className="sidebar" aria-label="Main navigation">
      <div className="sidebar__brand">Narratarr</div>
      <ul className="sidebar__list">
        {LINKS.map((link) => (
          <li key={link.to}>
            <NavLink
              to={link.to}
              className={({ isActive }) => "sidebar__link" + (isActive ? " sidebar__link--active" : "")}
            >
              <span aria-hidden="true">{link.icon}</span>
              <span>{link.label}</span>
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}
