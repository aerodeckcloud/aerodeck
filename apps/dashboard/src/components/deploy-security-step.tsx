import { Button } from "@/components/ui/button";
import type { SecurityReport } from "@/lib/deploy-draft";
import { NO_ADVISORIES_FOR_DIRECT_PINS, securityNeedsAcknowledgement } from "@/lib/deploy-draft";

export function DeploySecurityStep({
  report,
  loading,
  acknowledge,
  onAcknowledge,
  onRetry,
  disabled,
}: {
  report: SecurityReport | null;
  loading: boolean;
  acknowledge: boolean;
  onAcknowledge: (checked: boolean) => void;
  onRetry: () => void;
  disabled: boolean;
}) {
  if (loading) {
    return <p className="text-sm text-muted-foreground">Checking dependencies…</p>;
  }
  if (!report) {
    return (
      <div className="flex flex-col gap-3">
        <p className="text-sm text-muted-foreground">The dependency scan did not complete.</p>
        <Button type="button" variant="outline" size="sm" disabled={disabled} onClick={onRetry}>
          Retry
        </Button>
      </div>
    );
  }
  if (report.outcome === "no_manifest") {
    return (
      <p className="text-sm text-muted-foreground">There was no dependency manifest to scan.</p>
    );
  }
  if (report.outcome === "no_advisories") {
    return <p className="text-sm text-muted-foreground">{NO_ADVISORIES_FOR_DIRECT_PINS}</p>;
  }
  if (report.outcome === "incomplete") {
    return (
      <div className="flex flex-col gap-3">
        <p className="text-sm text-muted-foreground">The dependency scan did not complete.</p>
        <Button type="button" variant="outline" size="sm" disabled={disabled} onClick={onRetry}>
          Retry
        </Button>
        <Acknowledgement
          checked={acknowledge || report.acknowledgement === "incomplete"}
          disabled={disabled}
          onChange={onAcknowledge}
          label="The scan did not run. Continue anyway."
        />
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      <ul className="flex flex-col gap-2">
        {report.findings.map((finding) => (
          <li key={`${finding.package}-${finding.advisory_id}`} className="rounded-lg border border-border/80 p-3">
            <p className="font-mono text-xs">
              {finding.severity} · {finding.package} · {finding.advisory_id}
            </p>
            <p className="mt-1 text-sm text-muted-foreground">{finding.summary}</p>
          </li>
        ))}
      </ul>
      {securityNeedsAcknowledgement(report.outcome) ? (
        <Acknowledgement
          checked={acknowledge || report.acknowledgement === "findings"}
          disabled={disabled}
          onChange={onAcknowledge}
          label="I have seen these findings and want to continue."
        />
      ) : null}
    </div>
  );
}

function Acknowledgement({
  checked,
  disabled,
  onChange,
  label,
}: {
  checked: boolean;
  disabled: boolean;
  onChange: (checked: boolean) => void;
  label: string;
}) {
  return (
    <label className="flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        className="size-3.5 accent-primary"
      />
      {label}
    </label>
  );
}
