type AppNavigationProps = {
  currentPath: string;
  onNavigate: (path: string) => void;
  onSignOut?: () => void;
};

const links = [
  ["/", "경기 브리핑"],
  ["/history", "예측 기록"],
  ["/performance", "성능"],
  ["/operations", "운영"],
] as const;

export function AppNavigation({ currentPath, onNavigate, onSignOut }: AppNavigationProps) {
  return (
    <header className="topbar">
      <a
        className="brand"
        href="/"
        aria-label="Vlytics 홈"
        onClick={(event) => {
          event.preventDefault();
          onNavigate("/");
        }}
      >
        vlytics
      </a>
      <nav aria-label="주요 메뉴">
        {links.map(([path, label]) => (
          <a
            key={path}
            href={path}
            aria-current={currentPath === path ? "page" : undefined}
            onClick={(event) => {
              event.preventDefault();
              onNavigate(path);
            }}
          >
            {label}
          </a>
        ))}
      </nav>
      {onSignOut ? (
        <button type="button" className="secondary-button" onClick={onSignOut}>
          세션 종료
        </button>
      ) : null}
    </header>
  );
}
