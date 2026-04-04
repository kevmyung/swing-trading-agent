import { Card, CardContent } from '@/components/ui/card';

interface MetricCardProps {
  label: string;
  value: string;
  sub?: string;
  color?: 'gain' | 'loss' | 'default';
}

export function MetricCard({ label, value, sub, color = 'default' }: MetricCardProps) {
  const colorClass =
    color === 'gain' ? 'text-gain' :
    color === 'loss' ? 'text-loss' :
    'text-foreground';

  return (
    <Card>
      <CardContent className="pt-4 pb-3 px-4">
        <p className="text-xs text-muted-foreground tracking-wide uppercase">{label}</p>
        <p className={`text-xl font-semibold font-mono mt-1 ${colorClass}`}>{value}</p>
        {sub && <p className="text-xs text-muted-foreground mt-0.5">{sub}</p>}
      </CardContent>
    </Card>
  );
}
