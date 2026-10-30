/** The app mark: the same globe as the dock icon, drawn at header scale. */
export function LogoMark({ size = 20 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      className="shrink-0 text-foreground"
    >
      <circle cx="12" cy="12" r="8.75" stroke="currentColor" strokeWidth="1.4" />
      <ellipse cx="12" cy="12" rx="4.2" ry="8.75" stroke="currentColor" strokeWidth="1.4" />
      <path
        d="M3.4 12h17.2"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  )
}
