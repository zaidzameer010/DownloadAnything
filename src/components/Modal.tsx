import React, { useEffect } from 'react';

interface ModalProps {
  isOpen: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  footer?: React.ReactNode;
  size?: 'sm' | 'md' | 'lg' | 'xl';
}

export function Modal({ isOpen, title, onClose, children, footer, size = 'md' }: ModalProps) {
  // Prevent body scrolling when modal is open
  useEffect(() => {
    if (isOpen) {
      document.body.style.overflow = 'hidden';
    } else {
      document.body.style.overflow = '';
    }
    return () => {
      document.body.style.overflow = '';
    };
  }, [isOpen]);

  if (!isOpen) return null;

  return (
    <div className="reusable-modal-overlay" onClick={onClose}>
      <div className={`reusable-modal-box size-${size}`} onClick={(e) => e.stopPropagation()}>
        <div className="reusable-modal-header">
          <h3>{title}</h3>
          <button className="reusable-modal-close-btn" onClick={onClose}>&times;</button>
        </div>
        <div className="reusable-modal-body">
          {children}
        </div>
        {footer && (
          <div className="reusable-modal-footer">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}

export default Modal;
