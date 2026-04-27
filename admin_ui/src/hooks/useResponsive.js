/**
 * Responsive Design Hook - 2025 Best Practices
 * Mobile-first approach with content-based breakpoints
 * Based on 2025 device usage statistics and best practices
 */
import { useMediaQuery } from 'react-responsive';

// 2025 Standard Breakpoints (Mobile-First)
export const BREAKPOINTS = {
  // Mobile: 360px covers 95% of mobile devices
  mobile: 360,
  // Mobile Large: Modern large phones (iPhone Pro Max, etc.)
  mobileLarge: 430,
  // Tablet: Standard tablet portrait mode
  tablet: 768,
  // Tablet Landscape & Small Desktop
  tabletLandscape: 1024,
  // Desktop: Most common desktop resolution
  desktop: 1366,
  // Large Desktop: 4K and ultra-wide monitors
  desktopLarge: 1920
};

export const useResponsive = () => {
  // Mobile-first queries
  const isMobile = useMediaQuery({ maxWidth: BREAKPOINTS.mobile - 1 });
  const isMobileLarge = useMediaQuery({
    minWidth: BREAKPOINTS.mobile,
    maxWidth: BREAKPOINTS.mobileLarge - 1
  });
  const isTablet = useMediaQuery({
    minWidth: BREAKPOINTS.mobileLarge,
    maxWidth: BREAKPOINTS.tablet - 1
  });
  const isTabletLandscape = useMediaQuery({
    minWidth: BREAKPOINTS.tablet,
    maxWidth: BREAKPOINTS.tabletLandscape - 1
  });
  const isDesktop = useMediaQuery({
    minWidth: BREAKPOINTS.tabletLandscape,
    maxWidth: BREAKPOINTS.desktop - 1
  });
  const isDesktopLarge = useMediaQuery({ minWidth: BREAKPOINTS.desktop });

  // Grouped device queries for easier use
  const isMobileDevice = useMediaQuery({ maxWidth: BREAKPOINTS.mobileLarge - 1 });
  const isTabletDevice = useMediaQuery({
    minWidth: BREAKPOINTS.mobileLarge,
    maxWidth: BREAKPOINTS.tabletLandscape - 1
  });
  const isDesktopDevice = useMediaQuery({ minWidth: BREAKPOINTS.tabletLandscape });

  // Touch device detection
  const isTouchDevice = useMediaQuery({ query: '(hover: none) and (pointer: coarse)' });

  // Orientation detection
  const isLandscape = useMediaQuery({ orientation: 'landscape' });
  const isPortrait = useMediaQuery({ orientation: 'portrait' });

  // High DPI detection
  const isRetinaDevice = useMediaQuery({ query: '(-webkit-min-device-pixel-ratio: 2), (min-resolution: 192dpi)' });

  // Specific layout decisions
  const shouldUseMobileLayout = isMobileDevice;
  const shouldUseDrawerNavigation = isMobileDevice || (isTabletDevice && isPortrait);
  const shouldShowSidebar = isDesktopDevice;
  const shouldCollapseSidebar = isTabletDevice && isLandscape;

  // Grid system helpers
  const getGridColumns = () => {
    if (isMobileDevice) return 4;
    if (isTabletDevice) return 8;
    return 12;
  };

  const getContainerPadding = () => {
    if (isMobile) return '8px';
    if (isMobileLarge) return '12px';
    if (isTabletDevice) return '16px';
    if (isDesktop) return '24px';
    return '32px'; // Desktop large
  };

  const getFontSize = (mobile = '14px', tablet = '16px', desktop = '16px') => {
    if (isMobileDevice) return mobile;
    if (isTabletDevice) return tablet;
    return desktop;
  };

  // Ant Design specific breakpoint names
  const getAntBreakpoint = () => {
    if (isMobile) return 'xs';
    if (isMobileLarge) return 'sm';
    if (isTablet) return 'md';
    if (isTabletLandscape) return 'lg';
    return 'xl';
  };

  return {
    // Individual breakpoints
    isMobile,
    isMobileLarge,
    isTablet,
    isTabletLandscape,
    isDesktop,
    isDesktopLarge,

    // Device categories
    isMobileDevice,
    isTabletDevice,
    isDesktopDevice,

    // Device capabilities
    isTouchDevice,
    isLandscape,
    isPortrait,
    isRetinaDevice,

    // Layout decisions
    shouldUseMobileLayout,
    shouldUseDrawerNavigation,
    shouldShowSidebar,
    shouldCollapseSidebar,

    // Utilities
    getGridColumns,
    getContainerPadding,
    getFontSize,
    getAntBreakpoint,

    // Current breakpoint info
    currentBreakpoint: getAntBreakpoint(),

    // Breakpoint constants
    breakpoints: BREAKPOINTS
  };
};

export default useResponsive;
