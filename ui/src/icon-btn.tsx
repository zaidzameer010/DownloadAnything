import { Button, Tooltip } from '@heroui/react'

/** Icon-only button with tooltip — the shared chrome and row control. */
export function IconBtn({
  label,
  onPress,
  isDisabled,
  size = 'sm',
  variant = 'ghost',
  danger = false,
  className = '',
  children,
}: {
  label: string
  onPress?: () => void
  isDisabled?: boolean
  /** `md` is the header size; `sm` is the dense row size. */
  size?: 'sm' | 'md'
  variant?: 'ghost' | 'secondary'
  /** Destructive intent: the glyph turns danger on hover. */
  danger?: boolean
  className?: string
  children: React.ReactNode
}) {
  return (
    <Tooltip delay={400} closeDelay={0}>
      <Button
        isIconOnly
        variant={variant}
        size={size === 'md' ? 'md' : 'sm'}
        aria-label={label}
        isDisabled={isDisabled}
        onPress={onPress}
        className={`${danger ? 'hover:text-(--state-error-fg) hover:bg-danger-soft' : ''} ${className}`}
      >
        {children}
      </Button>
      <Tooltip.Content>{label}</Tooltip.Content>
    </Tooltip>
  )
}
