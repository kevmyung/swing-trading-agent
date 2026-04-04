interface Props {
  ticker: string;
  date?: string;
  className?: string;
}

export function TickerLink({ ticker, className }: Props) {
  return (
    <a
      href={`https://finance.yahoo.com/quote/${encodeURIComponent(ticker)}/`}
      target="_blank"
      rel="noopener noreferrer"
      className={`hover:underline hover:text-chart-1 transition-colors ${className ?? ''}`}
      onClick={(e) => e.stopPropagation()}
    >
      {ticker}
    </a>
  );
}
