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
  SettingOutlined,
  LogoutOutlined,
  MenuOutlined
} from '@ant-design/icons';
import { useAuthStore } from '../../stores/authStore';
import { useRealTimeWithFallback } from '../../hooks/useRealTimeUpdates';
import useResponsive from '../../hooks/useResponsive';

const { Header, Sider, Content } = Layout;
const { Title, Text } = Typography;

const AdminLayout = ({ children }) => {
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
    queries: ['dashboard', 'orders', 'users', 'products', 'deliveries'],
    onConnect: () => console.log('Real-time updates connected'),
    onDisconnect: () => console.log('Real-time updates disconnected'),
    onError: (error) => console.error('Real-time updates error:', error)
  });

  // Menu configuration
  const menuItems = [
    {
      key: '/dashboard',
      icon: <DashboardOutlined />,
      label: 'Dashboard'
    },
    {
      key: '/users',
      icon: <UserOutlined />,
      label: 'Users'
    },
    {
      key: '/orders',
      icon: <ShoppingCartOutlined />,
      label: 'Orders'
    },
    {
      key: '/products',
      icon: <ProductOutlined />,
      label: 'Products'
    },
    {
      key: '/delivery',
      icon: <TruckOutlined />,
      label: 'Delivery'
    },
    {
      key: '/loyalty',
      icon: <GiftOutlined />,
      label: 'Loyalty'
    },
    {
      key: '/notifications',
      icon: <BellOutlined />,
      label: 'Notifications'
    },
    {
      key: '/analytics',
      icon: <BarChartOutlined />,
      label: 'Analytics'
    },
    {
      key: '/settings',
      icon: <SettingOutlined />,
      label: 'Settings'
    }
  ];

  // User menu configuration
  const userMenuItems = [
    {
      key: 'profile',
      icon: <UserOutlined />,
      label: 'Profile'
    },
    {
      key: 'settings',
      icon: <SettingOutlined />,
      label: 'Settings'
    },
    {
      type: 'divider'
    },
    {
      key: 'logout',
      icon: <LogoutOutlined />,
      label: 'Logout',
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
    const currentItem = menuItems.find(item => item.key === location.pathname);
    return currentItem?.label || 'Dashboard';
  }, [location.pathname]);

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
        {collapsed ? 'BS' : 'Blue Stream'}
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

            {/* Right side */}
            <Space size={responsive.isMobileDevice ? 8 : 16}>
              {/* Connection Status - Desktop only */}
              {!responsive.isMobileDevice && (
                <Badge 
                  status={isConnected ? 'processing' : 'default'} 
                  text={connectionType || 'Offline'}
                />
              )}

              {/* Notifications - Hidden on very small screens */}
              {!responsive.isMobile && (
                <Badge count={0} size="small">
                  <Button 
                    type="text" 
                    icon={<BellOutlined />}
                    style={{
                      width: responsive.isTouchDevice ? '44px' : '40px',
                      height: responsive.isTouchDevice ? '44px' : '40px'
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
                <Space style={{ cursor: 'pointer' }}>
                  <Avatar
                    icon={<UserOutlined />}
                    src={user?.avatar}
                    size={responsive.isMobileDevice ? 32 : 40}
                    style={{ backgroundColor: '#1890ff' }}
                  />
                  {!responsive.isMobileDevice && (
                    <div style={{ textAlign: 'left' }}>
                      <Text 
                        strong 
                        style={{ 
                          fontSize: '14px',
                          display: 'block',
                          lineHeight: '1.2'
                        }}
                      >
                        {user?.first_name} {user?.last_name}
                      </Text>
                      <Text 
                        type="secondary" 
                        style={{ 
                          fontSize: '12px',
                          lineHeight: '1.2'
                        }}
                      >
                        {user?.role === 'super_admin' ? 'Super Admin' : 'Admin'}
                      </Text>
                    </div>
                  )}
                </Space>
              </Dropdown>
            </Space>
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