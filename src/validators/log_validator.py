"""
Validates Valkey node logs for affected shards
"""
import os
import re
import logging
from typing import List, Set, Optional
from dataclasses import dataclass
from ..models import NodeInfo, Operation

logger = logging.getLogger(__name__)

@dataclass
class LogFinding:
    node_id: str
    shard_id: int
    severity: str
    message: str
    log_line: str
    timestamp: Optional[str] = None

@dataclass
class LogValidationResult:
    success: bool
    findings: List[LogFinding]
    shards_checked: int
    
    def __str__(self):
        if self.success:
            return f"Log validation passed ({self.shards_checked} shards checked)"
        else:
            error_count = len([f for f in self.findings if f.severity == 'error'])
            warning_count = len([f for f in self.findings if f.severity == 'warning'])
            return f"Log validation failed: {error_count} errors, {warning_count} warnings"


class ShardLogValidator:
    """Validates logs for shards affected by operations and chaos"""
    
    def __init__(self):
        self.failover_promotion_patterns = [
            r"Failover election won",
            r"configEpoch set to \d+ after successful failover",
            r"Failover auth granted",
            r"manually failed over"
        ]
        
        self.split_brain_patterns = [r"multiple.*primary.*same.*shard"]
        
        self.stuck_election_patterns = [r"Currently unable to failover.*Waiting for votes"]
    
    def validate_affected_shards(self, cluster_nodes: List[NodeInfo], operations: List[Operation], killed_nodes: Set[str]) -> LogValidationResult:
        """Validate logs only for shards affected by operations or chaos"""
        
        logger.info("Starting node log validation for affected shards")
        
        findings = []
        affected_shards = self._get_affected_shards(operations, killed_nodes, cluster_nodes)
        
        logger.info(f"Validating logs for {len(affected_shards)} affected shards: {affected_shards}")
        
        for shard_id in affected_shards:
            shard_nodes = [n for n in cluster_nodes if n.shard_id == shard_id]
            
            # Check failover completion
            if self._shard_had_failover(shard_id, operations, cluster_nodes):
                logger.debug(f"Validating failover for shard {shard_id}")
                findings.extend(self._validate_failover(shard_nodes))
        
        for finding in findings:
            if finding.severity == 'error':
                logger.error(f"Log validation error in shard {finding.shard_id}: {finding.message}")
            elif finding.severity == 'warning':
                logger.warning(f"Log validation warning in shard {finding.shard_id}: {finding.message}")
        
        success = len([f for f in findings if f.severity == 'error']) == 0
        
        return LogValidationResult(success=success, findings=findings, shards_checked=len(affected_shards))
    
    def _get_affected_shards(self, operations: List[Operation], killed_nodes: Set[str], cluster_nodes: List[NodeInfo]) -> Set[int]:
        """Get set of shard IDs affected by operations or chaos"""
        affected = set()
        
        node_to_shard = {node.node_id: node.shard_id for node in cluster_nodes}
        
        for op in operations:
            shard_id = node_to_shard.get(op.target_node)
            if shard_id is not None:
                affected.add(shard_id)
        
        for node in cluster_nodes:
            node_addr = f"{node.host}:{node.port}"
            if node_addr in killed_nodes:
                affected.add(node.shard_id)
        
        return affected
    
    def _shard_had_failover(self, shard_id: int, operations: List[Operation], cluster_nodes: List[NodeInfo]) -> bool:
        """Check if shard had a failover operation"""
        node_to_shard = {node.node_id: node.shard_id for node in cluster_nodes}
        
        for op in operations:
            op_shard_id = node_to_shard.get(op.target_node)
            if op_shard_id == shard_id and op.type.value == 'failover':
                return True
        return False
    
    def _shard_had_kill(self, shard_id: int, killed_nodes: Set[str], cluster_nodes: List[NodeInfo]) -> bool:
        """Check if shard had a node killed"""
        for node in cluster_nodes:
            if node.shard_id == shard_id:
                node_addr = f"{node.host}:{node.port}"
                if node_addr in killed_nodes:
                    return True
        return False
    
    def _validate_failover(self, shard_nodes: List[NodeInfo]) -> List[LogFinding]:
        """Check failover completed successfully"""
        findings = []
        
        # Build mapping of Valkey's internal shard IDs to our shard_id
        valkey_shard_to_our_shard = {}
        for node in shard_nodes:
            log_lines = self._read_log_tail(node.log_file, lines=500) if os.path.exists(node.log_file) else []
            for line in log_lines:
                # Extract Valkey shard ID from formation messages
                match = re.search(r'is now part of shard ([a-f0-9]{40})', line)
                if match:
                    valkey_shard_id = match.group(1)
                    valkey_shard_to_our_shard[valkey_shard_id] = node.shard_id
                    break
        
        for node in shard_nodes:
            if not os.path.exists(node.log_file):
                logger.warning(f"Log file not found for node {node.node_id}: {node.log_file}")
                continue
            
            try:
                log_lines = self._read_log_tail(node.log_file, lines=200)
                
                # Check for failover success indicators on new primary
                if node.role == 'primary':
                    has_promotion = any(
                        self._has_pattern(log_lines, pattern)
                        for pattern in self.failover_promotion_patterns
                    )
                    
                    if not has_promotion:
                        findings.append(LogFinding(
                            node_id=node.node_id,
                            shard_id=node.shard_id,
                            severity='warning',
                            message='Primary has no failover promotion messages in recent logs',
                            log_line=''
                        ))
                    else:
                        # Check that promoted node is in correct shard
                        for line in log_lines:
                            match = re.search(r'Setting myself to primary in shard ([a-f0-9]{40})', line)
                            if match:
                                promoted_valkey_shard = match.group(1)
                                expected_shard_id = valkey_shard_to_our_shard.get(promoted_valkey_shard)
                                
                                if expected_shard_id is not None and expected_shard_id != node.shard_id:
                                    findings.append(LogFinding(
                                        node_id=node.node_id,
                                        shard_id=node.shard_id,
                                        severity='error',
                                        message=f'Node promoted to wrong shard: expected {node.shard_id}, got {expected_shard_id}',
                                        log_line=line.strip()
                                    ))
                                break
                
                # Check for split-brain indicators
                for pattern in self.split_brain_patterns:
                    matches = self._find_all_patterns(log_lines, pattern)
                    if matches:
                        findings.append(LogFinding(
                            node_id=node.node_id,
                            shard_id=node.shard_id,
                            severity='error',
                            message=f'Possible split-brain detected',
                            log_line=matches[0]
                        ))
            
            except Exception as e:
                logger.error(f"Error reading log for node {node.node_id}: {e}")
        
        return findings
    
    def _validate_kill_recovery(self, shard_nodes: List[NodeInfo], killed_nodes: Set[str]) -> List[LogFinding]:
        """Check cluster recovered from node kill"""
        findings = []
        
        for node in shard_nodes:
            node_addr = f"{node.host}:{node.port}"
            if node_addr in killed_nodes:
                continue  # Skip killed nodes
            
            if not os.path.exists(node.log_file):
                logger.warning(f"Log file not found for node {node.node_id}: {node.log_file}")
                continue
            
            try:
                log_lines = self._read_log_tail(node.log_file, lines=200)
                
                # Check for stuck election
                for pattern in self.stuck_election_patterns:
                    matches = self._find_all_patterns(log_lines, pattern)
                    if matches:
                        recent_lines = log_lines[-50:]
                        if any(match in line for line in recent_lines for match in matches):
                            findings.append(LogFinding(
                                node_id=node.node_id,
                                shard_id=node.shard_id,
                                severity='error',
                                message='Node may be stuck waiting for election',
                                log_line=matches[-1]
                            ))
            
            except Exception as e:
                logger.error(f"Error reading log for node {node.node_id}: {e}")
        
        return findings
    
    def _read_log_tail(self, log_file: str, lines: int = 100) -> List[str]:
        """Read last N lines from log file"""
        try:
            with open(log_file, 'r') as f:
                all_lines = f.readlines()
                return all_lines[-lines:] if len(all_lines) > lines else all_lines
        except Exception as e:
            logger.error(f"Error reading log file {log_file}: {e}")
            return []
    
    def _has_pattern(self, log_lines: List[str], pattern: str) -> bool:
        """Check if pattern exists in log lines"""
        regex = re.compile(pattern, re.IGNORECASE)
        return any(regex.search(line) for line in log_lines)
    
    def _find_all_patterns(self, log_lines: List[str], pattern: str) -> List[str]:
        """Find all lines matching pattern"""
        regex = re.compile(pattern, re.IGNORECASE)
        matches = []
        for line in log_lines:
            if regex.search(line):
                matches.append(line.strip())
        return matches
