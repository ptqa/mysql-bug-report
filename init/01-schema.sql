-- Minimal schema to reproduce the crash.
-- Only the columns referenced by the crashing query are included.

CREATE TABLE `calendar_events` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `eie` tinyint(1) DEFAULT '0',
  `status` varchar(50) DEFAULT NULL,
  `start_date` date DEFAULT NULL,
  `start_time` time DEFAULT NULL,
  `end_date` date DEFAULT NULL,
  `end_time` time DEFAULT NULL,
  `timezone` varchar(50) DEFAULT NULL,
  `title` varchar(255) DEFAULT NULL,
  `event_type` varchar(50) DEFAULT NULL,
  `staff_id` int DEFAULT NULL,
  `sfname` varchar(100) DEFAULT NULL,
  `slname` varchar(100) DEFAULT NULL,
  `inquiry_id` int DEFAULT NULL,
  `groupclients` json DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `calendar_events_inquiry_id_index` (`inquiry_id`),
  KEY `calendar_events_eie_index` (`eie`),
  KEY `calendar_events_groupclients_index` ((CAST(`groupclients` AS UNSIGNED ARRAY)))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
