import React, { useState, useCallback } from 'react';
import {
  Layout,
  Menu,
  Avatar,
  Dropdown,
  Button,
  Typography,
  Space,
  Drawer,
  Badge
} from 'antd';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  DashboardOutlined,
  UserOutlined,
  ShoppingCartOutlined,
  ProductOutlined,
  TruckOutlined,
  GiftOutlined,
  BellOutlined,
  BarChartOutlined,
  TranslationOutlined,
  SettingOutlined,
  LogoutOutlined,
  MenuOutlined,
  FileTextOutlined,
  TagsOutlined,
  TeamOutlined,
  BankOutlined
} from '@ant-design/icons';
import { useAuthStore } from '../../stores/authStore';
import { useRealTimeWithFallback } from '../../hooks/useRealTimeUpdates';
import useResponsive from '../../hooks/useResponsive';
import LanguageSwitcher from '../common/LanguageSwitcher';
import { useTranslation } from 'react-i18next';

const { Header, Sider, Content } = Layout;
const { Title, Text } = Typography;

const AdminLayout = ({ children }) => {
  // Load navigation namespace for ui.nav.* keys
  const { t } = useTranslation('navigation');
  const [mobileDrawerVisible, setMobileDrawerVisible] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();
  const { user, logout } = useAuthStore();
  const responsive = useResponsive();

  // Initialize real-time updates
  const { isConnected, connectionType } = useRealTimeWithFallback({
    enableWebSocket: true,
    enablePolling: true,
    pollingInterval: 30000,
    queries: ['dashboard', 'orders', 'users', 'products', 'deliveries', 'translations', 'loyalty-members', 'loyalty-programs', 'loyalty-rewards', 'analytics-loyalty'],
    onConnect: () => console.log('Real-time updates connected'),
    onDisconnect: () => console.log('Real-time updates disconnected'),
    onError: (error) => console.error('Real-time updates error:', error)
  });

  // Menu configuration
  const menuItems = [
    {
      key: '/dashboard',
      icon: <DashboardOutlined />,
      label: t('ui.nav.dashboard')
    },
    {
      key: '/users',
      icon: <UserOutlined />,
      label: t('ui.nav.users')
    },
    {
      key: '/orders',
      icon: <ShoppingCartOutlined />,
      label: t('ui.nav.orders')
    },
    {
      key: '/corporate-contracts',
      icon: <BankOutlined />,
      label: t('ui.nav.corporate_contracts', 'Corporate Contracts')
    },
    {
      key: '/products',
      icon: <ProductOutlined />,
      label: t('ui.nav.products')
    },
    {
      key: '/product-categories',
      icon: <TagsOutlined />,
      label: t('ui.nav.categories')
    },
    {
      key: '/delivery',
      icon: <TruckOutlined />,
      label: t('ui.nav.delivery'),
      children: [
        {
          key: '/delivery',
          label: t('ui.nav.deliveries')
        },
        {
          key: '/delivery-time-slots',
          label: t('ui.nav.time_slots')
        }
      ]
    },
    {
      key: '/loyalty',
      icon: <GiftOutlined />,
      label: t('ui.nav.loyalty'),
      children: [
        {
          key: '/loyalty/members',
          label: t('ui.nav.loyalty_members', { defaultValue: 'Members' })
        },
        {
          key: '/loyalty/programs',
          label: t('ui.nav.loyalty_programs', { defaultValue: 'Programs' })
        },
        {
          key: '/loyalty/rewards',
          label: t('ui.nav.loyalty_rewards', { defaultValue: 'Rewards' })
        }
      ]
    },
    {
      key: '/notifications',
      icon: <BellOutlined />,
      label: t('ui.nav.notifications')
    },
    {
      key: '/analytics',
      icon: <BarChartOutlined />,
      label: t('ui.nav.analytics')
    },
    {
      key: '/blog',
      icon: <FileTextOutlined />,
      label: t('ui.nav.blog')
    },
    {
      key: '/translations',
      icon: <TranslationOutlined />,
      label: t('ui.nav.translations')
    },
    {
      key: '/staff',
      icon: <TeamOutlined />,
      label: t('ui.nav.staff'),
      children: [
        {
          key: '/staff/delivery-persons',
          label: t('ui.nav.delivery_persons')
        },
        {
          key: '/staff/operators',
          label: t('ui.nav.operators')
        },
        {
          key: '/staff/management',
          label: t('ui.nav.staff_management')
        }
      ]
    },
    {
      key: '/settings',
      icon: <SettingOutlined />,
      label: t('ui.nav.settings')
    }
  ];

  // User menu configuration
  const userMenuItems = [
    {
      key: 'profile',
      icon: <UserOutlined />,
      label: t('ui.user_menu.profile')
    },
    {
      key: 'settings',
      icon: <SettingOutlined />,
      label: t('ui.user_menu.settings')
    },
    {
      type: 'divider'
    },
    {
      key: 'logout',
      icon: <LogoutOutlined />,
      label: t('ui.user_menu.logout'),
      onClick: handleLogout
    }
  ];

  // Event handlers
  const handleMenuClick = useCallback(({ key }) => {
    navigate(key);
    // Close mobile drawer after navigation
    if (responsive.shouldUseDrawerNavigation) {
      setMobileDrawerVisible(false);
    }
  }, [navigate, responsive.shouldUseDrawerNavigation]);

  function handleLogout() {
    logout();
    navigate('/login');
  }

  const toggleMobileDrawer = useCallback(() => {
    setMobileDrawerVisible(prev => !prev);
  }, []);

  const toggleSidebar = useCallback(() => {
    setSidebarCollapsed(prev => !prev);
  }, []);

  const closeMobileDrawer = useCallback(() => {
    setMobileDrawerVisible(false);
  }, []);

  // Get current page title
  const getPageTitle = useCallback(() => {
    const currentItem = menuItems.find((item) => (
      item.key === location.pathname
      || (item.children || []).some((child) => child.key === location.pathname)
    ));
    if (currentItem?.children?.length) {
      const child = currentItem.children.find((entry) => entry.key === location.pathname);
      return child?.label || currentItem.label;
    }
    return currentItem?.label || t('ui.nav.dashboard');
  }, [location.pathname, menuItems, t]);

  // Logo component
  const LogoComponent = ({ collapsed = false }) => (
    <div style={{
      height: 64,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      borderBottom: '1px solid #e8e8e8',
      padding: '0 16px'
    }}>
      <Title
        level={collapsed ? 4 : 3}
        style={{
          margin: 0,
          color: '#1890ff',
          fontSize: collapsed ? '16px' : '20px'
        }}
      >
        {collapsed ? t('ui.app_name_short') : t('ui.app_name_full')}
      </Title>
    </div>
  );

  // Navigation menu component
  const NavigationMenu = ({ mode = "inline" }) => (
    <Menu
      mode={mode}
      selectedKeys={[location.pathname]}
      items={menuItems}
      onClick={handleMenuClick}
      style={{
        border: 'none',
        fontSize: '14px'
      }}
    />
  );

  return (
    <Layout style={{ minHeight: '100vh' }}>
      {/* Mobile Drawer - Only shown on mobile */}
      {responsive.shouldUseDrawerNavigation && (
        <Drawer
          title={<LogoComponent />}
          placement="left"
          onClose={closeMobileDrawer}
          open={mobileDrawerVisible}
          bodyStyle={{ padding: 0 }}
          headerStyle={{ padding: 0, border: 'none' }}
          width={280}
          className="mobile-navigation-drawer"
        >
          <NavigationMenu />
        </Drawer>
      )}

      {/* Desktop/Tablet Sidebar - Only shown on larger screens */}
      {!responsive.shouldUseDrawerNavigation && (
        <Sider
          trigger={null}
          collapsible
          collapsed={sidebarCollapsed}
          className="admin-sider desktop-sider"
          width={280}
          collapsedWidth={80}
          style={{
            background: '#fff',
            borderRight: '1px solid #e8e8e8'
          }}
        >
          <LogoComponent collapsed={sidebarCollapsed} />
          <NavigationMenu />
        </Sider>
      )}

      {/* Main Layout */}
      <Layout>
        {/* Header - Responsive for all devices */}
        <Header
          className={`admin-header ${responsive.shouldUseDrawerNavigation ? 'mobile-header' : 'desktop-header'}`}
          style={{
            padding: responsive.shouldUseDrawerNavigation
              ? responsive.getContainerPadding()
              : `0 ${responsive.getContainerPadding()}`,
            height: responsive.shouldUseDrawerNavigation ? 'auto' : '64px',
            minHeight: responsive.shouldUseDrawerNavigation ? '64px' : '64px',
            background: '#fff',
            borderBottom: '1px solid #e8e8e8',
            boxShadow: '0 2px 8px rgba(0, 0, 0, 0.06)',
            display: 'flex',
            alignItems: 'center'
          }}
        >
          <div style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            width: '100%'
          }}>
            {/* Left side */}
            <Space size={responsive.isMobileDevice ? 12 : 16}>
              {/* Menu button for mobile OR collapse button for desktop */}
              <Button
                type="text"
                icon={<MenuOutlined />}
                onClick={responsive.shouldUseDrawerNavigation ? toggleMobileDrawer : toggleSidebar}
                style={{
                  fontSize: '16px',
                  width: responsive.isTouchDevice ? '44px' : '40px',
                  height: responsive.isTouchDevice ? '44px' : '40px'
                }}
              />

              {/* Page Title - Always visible on desktop, conditional on mobile */}
              <Title
                level={responsive.isMobileDevice ? 4 : 3}
                style={{
                  margin: 0,
                  fontSize: responsive.getFontSize('16px', '18px', '20px'),
                  display: responsive.isMobile ? 'none' : 'block'
                }}
              >
                {getPageTitle()}
              </Title>
            </Space>

            {/* Right side - Proper UX alignment */}
            <div style={{
              display: 'flex',
              alignItems: 'center',
              gap: responsive.isMobileDevice ? '8px' : '16px',
              marginLeft: 'auto' // Ensures right alignment
            }}>
              {/* Connection Status - Desktop only */}
              {!responsive.isMobileDevice && (
                <Badge
                  status={isConnected ? 'processing' : 'default'}
                  text={connectionType || t('ui.status.offline')}
                  style={{ whiteSpace: 'nowrap' }}
                />
              )}

              {/* Language Switcher */}
              <LanguageSwitcher
                showSyncButton={!responsive.isMobileDevice}
                size={responsive.isMobileDevice ? 'small' : 'middle'}
              />

              {/* Notifications - Hidden on very small screens */}
              {!responsive.isMobile && (
                <Badge count={0} size="small">
                  <Button
                    type="text"
                    icon={<BellOutlined />}
                    style={{
                      width: responsive.isTouchDevice ? '44px' : '40px',
                      height: responsive.isTouchDevice ? '44px' : '40px',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center'
                    }}
                  />
                </Badge>
              )}

              {/* User Dropdown */}
              <Dropdown
                menu={{ items: userMenuItems }}
                placement="bottomRight"
                trigger={['click']}
              >
                <div style={{
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '8px',
                  padding: '4px 8px',
                  borderRadius: '6px',
                  transition: 'background-color 0.2s',
                  ':hover': {
                    backgroundColor: '#f5f5f5'
                  }
                }}>
                  <Avatar
                    icon={<UserOutlined />}
                    src={user?.avatar}
                    size={responsive.isMobileDevice ? 32 : 40}
                    style={{ backgroundColor: '#1890ff' }}
                  />
                  {!responsive.isMobileDevice && (
                    <div style={{
                      textAlign: 'left',
                      minWidth: '120px' // Prevents layout shift
                    }}>
                      <Text
                        strong
                        style={{
                          fontSize: '14px',
                          display: 'block',
                          lineHeight: '1.2',
                          whiteSpace: 'wrap',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          maxWidth: '120px'
                        }}
                      >
                        {user?.first_name} {user?.last_name}
                      </Text>
                      <Text
                        type="secondary"
                        style={{
                          fontSize: '12px',
                          lineHeight: '1.2',
                          whiteSpace: 'nowrap'
                        }}
                      >
                        {user?.role === 'super_admin' ? t('ui.role.super_admin') : t('ui.role.admin')}
                      </Text>
                    </div>
                  )}
                </div>
              </Dropdown>
            </div>
          </div>

          {/* Mobile page title below header - Only on very small screens */}
          {responsive.isMobile && (
            <div style={{
              position: 'absolute',
              bottom: '-32px',
              left: '50%',
              transform: 'translateX(-50%)',
              textAlign: 'center',
              width: '100%',
              padding: '8px 16px',
              background: '#fff',
              borderBottom: '1px solid #f0f0f0'
            }}>
              <Text
                strong
                style={{
                  fontSize: '16px',
                  color: '#262626'
                }}
              >
                {getPageTitle()}
              </Text>
            </div>
          )}
        </Header>

        {/* Content Area */}
        <Content
          className={`admin-content ${responsive.shouldUseDrawerNavigation ? 'mobile-content' : 'desktop-content'}`}
          style={{
            padding: responsive.getContainerPadding(),
            marginTop: responsive.isMobile ? '32px' : '0', // Account for mobile title
            minHeight: `calc(100vh - 64px - ${responsive.isMobile ? '32px' : '0px'})`,
            background: '#f5f5f5'
          }}
        >
          <div className="fade-in">
            {children}
          </div>
        </Content>
      </Layout>
    </Layout>
  );
};

export default AdminLayout;
