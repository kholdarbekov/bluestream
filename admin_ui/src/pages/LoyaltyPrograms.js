import React, { useState } from 'react';
import {
  Table,
  Card,
  Button,
  Space,
  Tag,
  Dropdown,
  Modal,
  Form,
  Input,
  InputNumber,
  Row,
  Col,
  Statistic,
  message,
  Switch,
  Divider,
  Tabs
} from 'antd';
import {
  GiftOutlined,
  MoreOutlined,
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  EyeOutlined,
  WarningOutlined,
  TrophyOutlined,
  StarOutlined
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient } from 'react-query';
import adminService from '../services/adminService';

const { TextArea } = Input;
const { TabPane } = Tabs;

const LoyaltyPrograms = () => {
  const [selectedProgram, setSelectedProgram] = useState(null);
  const [isDetailModalVisible, setIsDetailModalVisible] = useState(false);
  const [isCreateModalVisible, setIsCreateModalVisible] = useState(false);
  const [isEditModalVisible, setIsEditModalVisible] = useState(false);
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();

  const queryClient = useQueryClient();

  // Fetch loyalty programs
  const { data, isLoading } = useQuery(
    'loyaltyPrograms',
    () => adminService.getLoyaltyPrograms(),
    {
      keepPreviousData: true
    }
  );

  // Create program mutation
  const createProgramMutation = useMutation(
    (programData) => adminService.createLoyaltyProgram(programData),
    {
      onSuccess: () => {
        message.success('Loyalty program created successfully');
        queryClient.invalidateQueries('loyaltyPrograms');
        setIsCreateModalVisible(false);
        createForm.resetFields();
      },
      onError: (error) => {
        const errorMessage = error.response?.data?.message || 'Failed to create program';
        message.error(errorMessage);
      }
    }
  );

  // Update program mutation
  const updateProgramMutation = useMutation(
    ({ programId, programData }) => adminService.updateLoyaltyProgram(programId, programData),
    {
      onSuccess: () => {
        message.success('Loyalty program updated successfully');
        queryClient.invalidateQueries('loyaltyPrograms');
        setIsEditModalVisible(false);
        editForm.resetFields();
      },
      onError: (error) => {
        const errorMessage = error.response?.data?.message || 'Failed to update program';
        message.error(errorMessage);
      }
    }
  );

  // Delete program mutation
  const deleteProgramMutation = useMutation(
    (programId) => adminService.deleteLoyaltyProgram(programId),
    {
      onSuccess: () => {
        message.success('Loyalty program deleted successfully');
        queryClient.invalidateQueries('loyaltyPrograms');
      },
      onError: (error) => {
        const errorMessage = error.response?.data?.message || 'Failed to delete program';
        message.error(errorMessage);
      }
    }
  );

  const columns = [
    {
      title: 'ID',
      dataIndex: 'id',
      key: 'id',
      width: 70
    },
    {
      title: 'Program Name',
      dataIndex: 'name',
      key: 'name',
      render: (text, record) => (
        <div>
          <div style={{ fontWeight: 'bold' }}>
            {text}
            {record.is_default && <Tag color="gold" style={{ marginLeft: 8 }}>DEFAULT</Tag>}
          </div>
          {record.description && (
            <small style={{ color: '#666' }}>{record.description.substring(0, 60)}...</small>
          )}
        </div>
      )
    },
    {
      title: 'Points Rate',
      dataIndex: 'points_per_uzs',
      key: 'points_per_uzs',
      width: 120,
      render: (rate) => (
        <Tag color="blue">{rate} pts/UZS</Tag>
      )
    },
    {
      title: 'Sign-up Bonus',
      dataIndex: 'signup_bonus',
      key: 'signup_bonus',
      width: 120,
      render: (bonus) => (
        <span>{bonus || 0} pts</span>
      )
    },
    {
      title: 'Expiry',
      dataIndex: 'points_expiry_days',
      key: 'points_expiry_days',
      width: 100,
      render: (days) => (
        <span>{days || 'Never'} days</span>
      )
    },
    {
      title: 'Status',
      dataIndex: 'is_active',
      key: 'is_active',
      width: 100,
      render: (is_active) => (
        <Tag color={is_active ? 'green' : 'red'}>
          {is_active ? 'Active' : 'Inactive'}
        </Tag>
      )
    },
    {
      title: 'Actions',
      key: 'actions',
      width: 100,
      render: (_, record) => (
        <Dropdown
          menu={{
            items: [
              {
                key: 'view',
                label: 'View Details',
                icon: <EyeOutlined />,
                onClick: () => handleViewProgram(record)
              },
              {
                key: 'edit',
                label: 'Edit Program',
                icon: <EditOutlined />,
                onClick: () => handleEditProgram(record)
              },
              {
                type: 'divider'
              },
              {
                key: 'delete',
                label: 'Delete Program',
                icon: <DeleteOutlined />,
                danger: true,
                disabled: record.is_default,
                onClick: () => handleDeleteProgram(record)
              }
            ]
          }}
          trigger={['click']}
        >
          <Button type="text" icon={<MoreOutlined />} />
        </Dropdown>
      )
    }
  ];

  const handleViewProgram = (program) => {
    setSelectedProgram(program);
    setIsDetailModalVisible(true);
  };

  const handleEditProgram = (program) => {
    setSelectedProgram(program);
    editForm.setFieldsValue({
      name: program.name,
      description: program.description,
      points_per_uzs: program.points_per_uzs,
      signup_bonus: program.signup_bonus,
      referral_bonus: program.referral_bonus,
      birthday_bonus: program.birthday_bonus,
      points_expiry_days: program.points_expiry_days,
      min_redemption_points: program.min_redemption_points,
      is_active: program.is_active,
      is_default: program.is_default
    });
    setIsEditModalVisible(true);
  };

  const handleDeleteProgram = (program) => {
    if (program.is_default) {
      message.warning('Cannot delete the default program');
      return;
    }

    Modal.confirm({
      title: 'Delete Loyalty Program?',
      content: `Are you sure you want to delete "${program.name}"? Programs with members will be deactivated instead.`,
      icon: <WarningOutlined />,
      okText: 'Delete',
      okType: 'danger',
      onOk: () => {
        deleteProgramMutation.mutate(program.id);
      }
    });
  };

  const handleCreateSubmit = (values) => {
    createProgramMutation.mutate(values);
  };

  const handleEditSubmit = (values) => {
    updateProgramMutation.mutate({
      programId: selectedProgram.id,
      programData: values
    });
  };

  // Calculate summary statistics
  const programs = data?.data?.programs || [];
  const totalPrograms = programs.length;
  const activePrograms = programs.filter(p => p.is_active).length;
  const defaultProgram = programs.find(p => p.is_default);

  return (
    <div>
      {/* Summary Cards */}
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title="Total Programs"
              value={totalPrograms}
              prefix={<GiftOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title="Active Programs"
              value={activePrograms}
              valueStyle={{ color: '#52c41a' }}
              prefix={<StarOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title="Default Program"
              value={defaultProgram?.name || 'None'}
              prefix={<TrophyOutlined />}
              valueStyle={{ fontSize: '18px' }}
            />
          </Card>
        </Col>
      </Row>

      <Card>
        {/* Action Button */}
        <div className="table-actions">
          <Space wrap />
          <Space>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => setIsCreateModalVisible(true)}
            >
              Add Loyalty Program
            </Button>
          </Space>
        </div>

        {/* Programs Table */}
        <Table
          columns={columns}
          dataSource={programs}
          loading={isLoading}
          rowKey="id"
          pagination={false}
          className="admin-table"
        />
      </Card>

      {/* Program Details Modal */}
      <Modal
        title={`Program Details - ${selectedProgram?.name}`}
        open={isDetailModalVisible}
        onCancel={() => setIsDetailModalVisible(false)}
        footer={null}
        width={700}
      >
        {selectedProgram && (
          <div>
            <Row gutter={16}>
              <Col span={24}>
                <h3>
                  {selectedProgram.name}
                  {selectedProgram.is_default && <Tag color="gold" style={{ marginLeft: 8 }}>DEFAULT</Tag>}
                  <Tag color={selectedProgram.is_active ? 'green' : 'red'} style={{ marginLeft: 8 }}>
                    {selectedProgram.is_active ? 'Active' : 'Inactive'}
                  </Tag>
                </h3>
                <p><strong>ID:</strong> {selectedProgram.id}</p>
              </Col>
            </Row>

            {selectedProgram.description && (
              <>
                <Divider>Description</Divider>
                <p>{selectedProgram.description}</p>
              </>
            )}

            <Divider>Program Settings</Divider>
            <Row gutter={16}>
              <Col span={12}>
                <p><strong>Points per UZS:</strong> {selectedProgram.points_per_uzs}</p>
                <p><strong>Sign-up Bonus:</strong> {selectedProgram.signup_bonus || 0} pts</p>
                <p><strong>Referral Bonus:</strong> {selectedProgram.referral_bonus || 0} pts</p>
              </Col>
              <Col span={12}>
                <p><strong>Birthday Bonus:</strong> {selectedProgram.birthday_bonus || 0} pts</p>
                <p><strong>Points Expiry:</strong> {selectedProgram.points_expiry_days || 'Never'} days</p>
                <p><strong>Min Redemption:</strong> {selectedProgram.min_redemption_points || 0} pts</p>
              </Col>
            </Row>

            <div style={{ marginTop: 16, textAlign: 'right' }}>
              <Space>
                <Button
                  type="primary"
                  onClick={() => {
                    setIsDetailModalVisible(false);
                    handleEditProgram(selectedProgram);
                  }}
                >
                  Edit Program
                </Button>
                <Button onClick={() => setIsDetailModalVisible(false)}>
                  Close
                </Button>
              </Space>
            </div>
          </div>
        )}
      </Modal>

      {/* Create Program Modal */}
      <Modal
        title="Add New Loyalty Program"
        open={isCreateModalVisible}
        onCancel={() => setIsCreateModalVisible(false)}
        footer={null}
        width={700}
      >
        <Form
          form={createForm}
          layout="vertical"
          onFinish={handleCreateSubmit}
        >
          <Tabs defaultActiveKey="1">
            <TabPane tab="Basic Info" key="1">
              <Form.Item
                name="name"
                label="Program Name"
                rules={[{ required: true, message: 'Please enter program name' }]}
              >
                <Input placeholder="Enter program name" />
              </Form.Item>

              <Form.Item
                name="description"
                label="Description"
              >
                <TextArea
                  rows={3}
                  placeholder="Enter program description..."
                />
              </Form.Item>

              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item
                    name="is_active"
                    label="Active"
                    valuePropName="checked"
                    initialValue={true}
                  >
                    <Switch />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item
                    name="is_default"
                    label="Set as Default"
                    valuePropName="checked"
                    initialValue={false}
                  >
                    <Switch />
                  </Form.Item>
                </Col>
              </Row>
            </TabPane>

            <TabPane tab="Points & Rewards" key="2">
              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item
                    name="points_per_uzs"
                    label="Points per UZS"
                    initialValue={1}
                    rules={[{ required: true, message: 'Please enter points rate' }]}
                  >
                    <InputNumber
                      placeholder="1"
                      style={{ width: '100%' }}
                      min={0}
                      step={0.1}
                    />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item
                    name="min_redemption_points"
                    label="Min Redemption Points"
                    initialValue={100}
                  >
                    <InputNumber
                      placeholder="100"
                      style={{ width: '100%' }}
                      min={0}
                    />
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item
                    name="signup_bonus"
                    label="Sign-up Bonus (pts)"
                    initialValue={0}
                  >
                    <InputNumber
                      placeholder="0"
                      style={{ width: '100%' }}
                      min={0}
                    />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item
                    name="referral_bonus"
                    label="Referral Bonus (pts)"
                    initialValue={0}
                  >
                    <InputNumber
                      placeholder="0"
                      style={{ width: '100%' }}
                      min={0}
                    />
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item
                    name="birthday_bonus"
                    label="Birthday Bonus (pts)"
                    initialValue={0}
                  >
                    <InputNumber
                      placeholder="0"
                      style={{ width: '100%' }}
                      min={0}
                    />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item
                    name="points_expiry_days"
                    label="Points Expiry (days)"
                    initialValue={365}
                  >
                    <InputNumber
                      placeholder="365"
                      style={{ width: '100%' }}
                      min={0}
                    />
                  </Form.Item>
                </Col>
              </Row>
            </TabPane>
          </Tabs>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right', marginTop: 16 }}>
            <Space>
              <Button onClick={() => setIsCreateModalVisible(false)}>
                Cancel
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={createProgramMutation.isLoading}
              >
                Create Program
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* Edit Program Modal */}
      <Modal
        title={`Edit Program - ${selectedProgram?.name}`}
        open={isEditModalVisible}
        onCancel={() => setIsEditModalVisible(false)}
        footer={null}
        width={700}
      >
        <Form
          form={editForm}
          layout="vertical"
          onFinish={handleEditSubmit}
        >
          <Tabs defaultActiveKey="1">
            <TabPane tab="Basic Info" key="1">
              <Form.Item
                name="name"
                label="Program Name"
                rules={[{ required: true, message: 'Please enter program name' }]}
              >
                <Input placeholder="Enter program name" />
              </Form.Item>

              <Form.Item
                name="description"
                label="Description"
              >
                <TextArea
                  rows={3}
                  placeholder="Enter program description..."
                />
              </Form.Item>

              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item
                    name="is_active"
                    label="Active"
                    valuePropName="checked"
                  >
                    <Switch />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item
                    name="is_default"
                    label="Set as Default"
                    valuePropName="checked"
                  >
                    <Switch />
                  </Form.Item>
                </Col>
              </Row>
            </TabPane>

            <TabPane tab="Points & Rewards" key="2">
              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item
                    name="points_per_uzs"
                    label="Points per UZS"
                    rules={[{ required: true, message: 'Please enter points rate' }]}
                  >
                    <InputNumber
                      placeholder="1"
                      style={{ width: '100%' }}
                      min={0}
                      step={0.1}
                    />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item
                    name="min_redemption_points"
                    label="Min Redemption Points"
                  >
                    <InputNumber
                      placeholder="100"
                      style={{ width: '100%' }}
                      min={0}
                    />
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item
                    name="signup_bonus"
                    label="Sign-up Bonus (pts)"
                  >
                    <InputNumber
                      placeholder="0"
                      style={{ width: '100%' }}
                      min={0}
                    />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item
                    name="referral_bonus"
                    label="Referral Bonus (pts)"
                  >
                    <InputNumber
                      placeholder="0"
                      style={{ width: '100%' }}
                      min={0}
                    />
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item
                    name="birthday_bonus"
                    label="Birthday Bonus (pts)"
                  >
                    <InputNumber
                      placeholder="0"
                      style={{ width: '100%' }}
                      min={0}
                    />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item
                    name="points_expiry_days"
                    label="Points Expiry (days)"
                  >
                    <InputNumber
                      placeholder="365"
                      style={{ width: '100%' }}
                      min={0}
                    />
                  </Form.Item>
                </Col>
              </Row>
            </TabPane>
          </Tabs>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right', marginTop: 16 }}>
            <Space>
              <Button onClick={() => setIsEditModalVisible(false)}>
                Cancel
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={updateProgramMutation.isLoading}
              >
                Update Program
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default LoyaltyPrograms;
