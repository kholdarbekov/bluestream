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
  MenuOutlined,
  CloseOutlined
} from '@ant-design/icons';
import { useAuthStore } from '../../stores/authStore';
import { useRealTimeWithFallback } from '../../hooks/useRealTimeUpdates';
import useResponsive from '../../hooks/useResponsive';

const { Header, Sider, Content } = Layout;
const { Title, Text } = Typography;

const AdminLayout = ({ children }) => {
  const [mobileDrawerVisible, setMobileDrawerVisible] = useState(false);
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
      padding: responsive.isMobileDevice ? '0 16px' : '0 24px'
    }}>
      <Title 
        level={collapsed ? 4 : 3} 
        style={{ 
          margin: 0, 
          color: '#1890ff',
          fontSize: responsive.isMobileDevice ? '18px' : (collapsed ? '16px' : '20px')
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
        fontSize: responsive.isMobileDevice ? '16px' : '14px'
      }}
    />
  );

  // Mobile drawer implementation
  if (responsive.shouldUseDrawerNavigation) {
    return (
      <Layout style={{ minHeight: '100vh' }}>
        {/* Mobile Drawer */}
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

        {/* Main Layout */}
        <Layout>
          {/* Mobile Header */}
          <Header 
            className="admin-header mobile-header"
            style={{ 
              padding: responsive.getContainerPadding(),
              height: 'auto',
              minHeight: '64px'
            }}
          >
            <div style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              height: '100%'
            }}>
              {/* Left side - Menu button and title */}
              <Space size={12}>
                <Button
                  type="text"
                  icon={<MenuOutlined />}
                  onClick={toggleMobileDrawer}
                  size={responsive.isMobile ? 'large' : 'middle'}
                  style={{ 
                    fontSize: '18px',
                    width: '44px',
                    height: '44px'
                  }}
                />
                {!responsive.isMobile && (
                  <Title 
                    level={4} 
                    style={{ 
                      margin: 0,
                      fontSize: responsive.getFontSize('16px', '18px', '18px')
                    }}
                  >
                    {getPageTitle()}
                  </Title>
                )}
              </Space>

              {/* Right side - User menu */}
              <Space size={responsive.isMobile ? 8 : 12}>
                {/* Notifications - hidden on very small screens */}
                {!responsive.isMobile && (
                  <Badge count={0} size="small">
                    <Button 
                      type="text" 
                      icon={<BellOutlined />}
                      size="large"
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
                      size={responsive.isMobile ? 32 : 40}
                      style={{ backgroundColor: '#1890ff' }}
                    />
                    {!responsive.isMobile && (
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

            {/* Mobile page title below header */}
            {responsive.isMobile && (
              <div style={{ 
                textAlign: 'center',
                paddingTop: '8px',
                borderTop: '1px solid #f0f0f0',
                marginTop: '8px'
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

          {/* Mobile Content */}
          <Content 
            className="admin-content mobile-content"
            style={{ 
              padding: responsive.getContainerPadding(),
              minHeight: 'calc(100vh - 64px)',
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
  }

  // Desktop/Tablet Layout
  return (
    <Layout style={{ minHeight: '100vh' }}>
      {/* Desktop Sidebar */}
      <Sider
        trigger={null}
        collapsible
        collapsed={responsive.shouldCollapseSidebar}
        className="admin-sider desktop-sider"
        width={280}
        collapsedWidth={80}
        breakpoint="lg"
        onBreakpoint={(broken) => {
          // This handles the automatic collapse on smaller screens
          console.log('Breakpoint triggered:', broken);
        }}
        style={{
          background: '#fff',
          borderRight: '1px solid #e8e8e8'
        }}
      >
        <LogoComponent collapsed={responsive.shouldCollapseSidebar} />
        <NavigationMenu />
      </Sider>

      {/* Desktop Layout */}
      <Layout>
        {/* Desktop Header */}
        <Header 
          className="admin-header desktop-header"
          style={{ 
            padding: `0 ${responsive.getContainerPadding()}`,
            background: '#fff',
            borderBottom: '1px solid #e8e8e8',
            boxShadow: '0 2px 8px rgba(0, 0, 0, 0.06)'
          }}
        >
          <div style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            height: '100%'
          }}>
            {/* Page Title */}
            <Title 
              level={3} 
              style={{ 
                margin: 0,
                color: '#262626'
              }}
            >
              {getPageTitle()}
            </Title>

            {/* Desktop Header Actions */}
            <Space size={16}>
              {/* Connection Status Indicator */}
              <Badge 
                status={isConnected ? 'processing' : 'default'} 
                text={connectionType || 'Offline'}
              />

              {/* Notifications */}
              <Badge count={0} size="small">
                <Button 
                  type="text" 
                  icon={<BellOutlined />}
                  size="large"
                />
              </Badge>

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
                    size={40}
                    style={{ backgroundColor: '#1890ff' }}
                  />
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
                </Space>
              </Dropdown>
            </Space>
          </div>
        </Header>

        {/* Desktop Content */}
        <Content 
          className="admin-content desktop-content"
          style={{ 
            padding: responsive.getContainerPadding(),
            minHeight: 'calc(100vh - 64px)',
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