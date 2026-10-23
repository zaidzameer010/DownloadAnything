import { Button, Tooltip } from '@heroui/react'

/** Icon-only button with tooltip — the shared chrome and row control. */
export function IconBtn({
  label,
  onPress,
  isDisabled,
  size = 'sm',
  variant = 'ghost',
  className = '',
  children,
}: {
  label: string
  onPress?: () => void
  isDisabled?: boolean
  /** `md` is the header/toolbar size; `sm` is the dense row size. */
  size?: 'sm' | 'md'
  variant?: 'ghost' | 'secondary'
  className?: string
  children: React.ReactNode
}) {
  return (
    <Tooltip delay={400} closeDelay={0}>
      <Button
        isIconOnly
        variant={variant}
        size="sm"
        aria-label={label}
        isDisabled={isDisabled}
        onPress={onPress}
        className={`press-tactile ${
          size === 'md' ? 'size-8.5 rounded-lg' : 'size-7.5 rounded-md'
        } shrink-0 text-muted hover:text-foreground active:bg-(--state-active-bg) ${className}`}
      >
        {children}
      </Button>
      <Tooltip.Content>{label}</Tooltip.Content>
    </Tooltip>
  )
}
