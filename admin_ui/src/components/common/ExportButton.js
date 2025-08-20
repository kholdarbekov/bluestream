import React, { useState } from 'react';
import { Button, Dropdown, message } from 'antd';
import { ExportOutlined, FileExcelOutlined, FilePdfOutlined, FileTextOutlined } from '@ant-design/icons';
import exportUtils from '../../utils/exportUtils';

const ExportButton = ({
  type,
  filters = {},
  data = null,
  filename = null,
  title = 'Export',
  size = 'default',
  disabled = false
}) => {
  const [loading, setLoading] = useState(false);

  const handleExport = async (format) => {
    setLoading(true);

    try {
      let result;

      if (data) {
        // Export provided data directly
        const exportFilename = filename || `export_${new Date().toISOString().split('T')[0]}`;

        if (format === 'excel') {
          result = exportUtils.exportToExcel(data, exportFilename);
        } else if (format === 'csv') {
          result = exportUtils.exportToCSV(data, exportFilename);
        } else if (format === 'pdf') {
          result = exportUtils.exportToPDF(data, exportFilename, title);
        }
      } else if (type) {
        // Export from API
        result = await exportUtils.exportData(type, filters, format);
      } else {
        throw new Error('Either type or data must be provided');
      }

      if (result.success) {
        message.success(result.message);
      } else {
        message.error(result.message);
      }
    } catch (error) {
      console.error('Export error:', error);
      message.error('Export failed. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  const menuItems = [
    {
      key: 'excel',
      icon: <FileExcelOutlined />,
      label: 'Excel (.xlsx)',
      onClick: () => handleExport('excel')
    },
    {
      key: 'csv',
      icon: <FileTextOutlined />,
      label: 'CSV (.csv)',
      onClick: () => handleExport('csv')
    },
    {
      key: 'pdf',
      icon: <FilePdfOutlined />,
      label: 'PDF (.pdf)',
      onClick: () => handleExport('pdf')
    }
  ];

  return (
    <Dropdown
      menu={{ items: menuItems }}
      trigger={['click']}
      disabled={disabled || loading}
    >
      <Button
        icon={<ExportOutlined />}
        loading={loading}
        size={size}
        disabled={disabled}
      >
        {title}
      </Button>
    </Dropdown>
  );
};

export default ExportButton;