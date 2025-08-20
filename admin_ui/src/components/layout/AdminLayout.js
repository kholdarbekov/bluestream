import React, { useState } from 'react';
import { Layout, Menu, Avatar, Dropdown, Button, Typography, Space } from 'antd';
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
  MenuFoldOutlined,
  MenuUnfoldOutlined
} from '@ant-design/icons';
import { useAuthStore } from '../../stores/authStore';
import { useRealTimeWithFallback } from '../../hooks/useRealTimeUpdates';

const { Header, Sider, Content } = Layout;
const { Title, Text } = Typography;

const AdminLayout = ({ children }) => {
  const [collapsed, setCollapsed] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();
  const { user, logout } = useAuthStore();

  // Initialize real-time updates
  const { isConnected, connectionType } = useRealTimeWithFallback({
    enableWebSocket: true,
    enablePolling: true,
    pollingInterval: 30000,
    queries: ['dashboard', 'orders', 'users', 'products', 'deliveries'],
    onConnect: () => {
      console.log('Real-time updates connected');
    },
    onDisconnect: () => {
      console.log('Real-time updates disconnected');
    },
    onError: (error) => {
      console.error('Real-time updates error:', error);
    }
  });

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

  const handleMenuClick = ({ key }) => {
    navigate(key);
  };

  const handleLogout = () => {
    logout();
    navigate('/login');
  };

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

  const getPageTitle = () => {
    const currentItem = menuItems.find(item => item.key === location.pathname);
    return currentItem?.label || 'Dashboard';
  };

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider
        trigger={null}
        collapsible
        collapsed={collapsed}
        className="admin-sider"
        width={250}
      >
        <div style={{
          height: 64,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          borderBottom: '1px solid #e8e8e8'
        }}>
          {!collapsed ? (
            <Title level={3} style={{ margin: 0, color: '#1890ff' }}>
              Blue Stream
            </Title>
          ) : (
            <Title level={4} style={{ margin: 0, color: '#1890ff' }}>
              BS
            </Title>
          )}
        </div>
        <Menu
          mode="inline"
          selectedKeys={[location.pathname]}
          items={menuItems}
          onClick={handleMenuClick}
          style={{ border: 'none' }}
        />
      </Sider>

      <Layout>
        <Header className="admin-header" style={{ padding: '0 24px' }}>
          <div style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center'
          }}>
            <Space>
              <Button
                type="text"
                icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
                onClick={() => setCollapsed(!collapsed)}
                style={{ fontSize: '16px', width: 64, height: 64 }}
              />
              <Title level={4} style={{ margin: 0 }}>
                {getPageTitle()}
              </Title>
            </Space>

            <Space>
              <Button type="text" icon={<BellOutlined />} />
              <Dropdown
                menu={{ items: userMenuItems }}
                placement="bottomRight"
                trigger={['click']}
              >
                <Space style={{ cursor: 'pointer' }}>
                  <Avatar
                    icon={<UserOutlined />}
                    src={user?.avatar}
                    style={{ backgroundColor: '#1890ff' }}
                  />
                  <div style={{ display: !collapsed ? 'block' : 'none' }}>
                    <Text strong>{user?.first_name} {user?.last_name}</Text>
                    <br />
                    <Text type="secondary" style={{ fontSize: '12px' }}>
                      {user?.role === 'super_admin' ? 'Super Admin' : 'Admin'}
                    </Text>
                  </div>
                </Space>
              </Dropdown>
            </Space>
          </div>
        </Header>

        <Content className="admin-content">
          <div className="fade-in">
            {children}
          </div>
        </Content>
      </Layout>
    </Layout>
  );
};

export default AdminLayout;