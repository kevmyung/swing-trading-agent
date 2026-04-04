import { Link, Outlet, useLocation } from 'react-router-dom';

function NavLink({ to, label, active }: { to: string; label: string; active: boolean }) {
  return (
    <Link
      to={to}
      className={`px-3 py-1.5 rounded-md text-sm transition-colors ${
        active
          ? 'bg-secondary text-secondary-foreground font-medium'
          : 'text-muted-foreground hover:text-foreground'
      }`}
    >
      {label}
    </Link>
  );
}

function DropdownItem({ to, label, active }: { to: string; label: string; active: boolean }) {
  return (
    <Link
      to={to}
      className={`block px-3 py-1.5 text-sm transition-colors ${
        active
          ? 'bg-secondary text-secondary-foreground font-medium'
          : 'text-muted-foreground hover:text-foreground hover:bg-secondary/50'
      }`}
    >
      {label}
    </Link>
  );
}

function NavDropdown({
  label,
  active,
  items,
}: {
  label: string;
  active: boolean;
  items: { to: string; label: string; active: boolean }[];
}) {
  return (
    <div className="relative group">
      <button
        className={`px-3 py-1.5 rounded-md text-sm transition-colors ${
          active
            ? 'bg-secondary text-secondary-foreground font-medium'
            : 'text-muted-foreground hover:text-foreground'
        }`}
      >
        {label}
      </button>
      <div className="absolute left-0 top-full pt-1 hidden group-hover:block z-50">
        <div className="bg-card border border-border rounded-md shadow-md py-1 min-w-[140px]">
          {items.map((item) => (
            <DropdownItem key={item.to} {...item} />
          ))}
        </div>
      </div>
    </div>
  );
}

export function Layout() {
  const { pathname } = useLocation();

  const isBacktest = pathname === '/' || pathname === '/new' || pathname === '/analysis' || pathname === '/data' || pathname.startsWith('/sessions/');
  const isLive = pathname.startsWith('/live');

  return (
    <div className="min-h-screen">
      <header className="border-b border-border bg-card/80 backdrop-blur-sm sticky top-0 z-40">
        <div className="max-w-[1400px] mx-auto px-6 h-12 flex items-center gap-8">
          <Link to="/" className="text-base font-bold tracking-tight">
            Auto Swing Trader
          </Link>
          <nav className="flex gap-1">
            <NavDropdown
              label="Backtest"
              active={isBacktest}
              items={[
                { to: '/new', label: 'New', active: pathname === '/new' },
                { to: '/', label: 'History', active: pathname === '/' || pathname.startsWith('/sessions/') },
                { to: '/analysis', label: 'Analysis', active: pathname === '/analysis' },
                { to: '/data', label: 'Data', active: pathname === '/data' },
              ]}
            />
            <NavDropdown
              label="Live"
              active={isLive}
              items={[
                { to: '/live/paper', label: 'Paper Trading', active: pathname === '/live/paper' },
                { to: '/live/real', label: 'Live Trading', active: pathname === '/live/real' },
              ]}
            />
            <NavLink to="/settings" label="Settings" active={pathname === '/settings'} />
          </nav>
        </div>
      </header>
      <main className="max-w-[1400px] mx-auto px-6 py-6">
        <Outlet />
      </main>
    </div>
  );
}
