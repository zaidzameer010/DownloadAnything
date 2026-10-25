export function LogoMark({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <rect x="1.5" y="1.5" width="21" height="21" rx="6" fill="var(--accent)" />
      <path
        d="M9 7.5v9l7-4.5-7-4.5Z"
        fill="var(--accent-foreground)"
        stroke="var(--accent-foreground)"
        strokeWidth="1"
        strokeLinejoin="round"
      />
      <path
        d="M18.5 7.5v9"
        stroke="var(--accent-foreground)"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  )
}
