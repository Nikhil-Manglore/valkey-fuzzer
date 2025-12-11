"""
Parallel Executor - Executes multiple operations concurrently with buffered logging
"""
import logging
import time
from typing import List, Tuple
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from ..models import Operation, ChaosConfig, ChaosResult, ChaosType
from .operation_log_buffer import OperationLogBuffer

logger = logging.getLogger()

class ParallelExecutor:
    """Executes operations in parallel by shard with per-operation log buffering"""
    
    def __init__(self, operation_orchestrator, chaos_coordinator, fuzzer_logger, state_validator=None):
        self.operation_orchestrator = operation_orchestrator
        self.chaos_coordinator = chaos_coordinator
        self.fuzzer_logger = fuzzer_logger
        self.state_validator = state_validator
    
    def execute_operations_parallel(self, operations: List[Operation], chaos_config: ChaosConfig, cluster_connection, cluster_id: str) -> Tuple[int, List[ChaosResult]]:
        """Execute operations in parallel by shard with buffered logging"""
        
        logger.info(f"Starting parallel execution of {len(operations)} operations")
        
        # Group operations by shard
        shard_groups = defaultdict(list)
        for i, op in enumerate(operations):
            shard_id = op.target_node.rsplit('-', 1)[0] if '-' in op.target_node else op.target_node
            shard_groups[shard_id].append((i, op))
        
        logger.info(f"Grouped {len(operations)} operations into {len(shard_groups)} shard groups")
        
        operations_executed = 0
        all_chaos_events = []
        results = [None] * len(operations)
        
        def execute_shard_operations(shard_id: str, shard_ops: List[Tuple[int, Operation]]):
            """Execute all operations for a shard sequentially"""
            shard_results = []
            for op_index, operation in shard_ops:
                op_id = f"{op_index + 1}: {operation.type.value} on {operation.target_node}"
                buffer = OperationLogBuffer(op_id)
                
                try:
                    buffer.info(f"Starting {operation.type.value}")
                    
                    # Coordinate chaos (may return deferred chaos for after-operation)
                    chaos_results = self.chaos_coordinator.coordinate_chaos_with_operation(
                        operation,
                        chaos_config,
                        cluster_connection,
                        cluster_id,
                        log_buffer=buffer
                    )
                    
                    # Separate immediate and deferred chaos
                    immediate_chaos = [c for c in chaos_results if not isinstance(c, dict) or not c.get('deferred')]
                    deferred_chaos = [c for c in chaos_results if isinstance(c, dict) and c.get('deferred')]
                    
                    # Execute operation with buffered logging
                    success = self.operation_orchestrator.execute_operation(
                        operation,
                        cluster_id,
                        log_buffer=buffer
                    )
                    
                    # Inject deferred chaos after operation completes
                    for deferred in deferred_chaos:
                        buffer.info(f"Injecting deferred chaos after operation (delay: {deferred['delay']:.2f}s)")
                        time.sleep(deferred['delay'])
                        result = self.chaos_coordinator._inject_chaos(
                            deferred['target_node'],
                            deferred['chaos_config'],
                            deferred['should_randomize'],
                            buffer
                        )
                        immediate_chaos.append(result)
                    
                    chaos_events = immediate_chaos
                    
                    # Log operation result
                    self.fuzzer_logger.log_operation(
                        operation,
                        success,
                        f"Operation {'succeeded' if success else 'failed'}",
                        silent=False
                    )
                    
                    buffer.info(f"Operation {'succeeded' if success else 'failed'}")
                    
                    # Validate cluster state after operation if validator is provided
                    validation_result = None
                    if self.state_validator and success:
                        validation_result = self.state_validator.validate_with_retry(
                            cluster_connection,
                            None,
                            None
                        )
                        self._add_validation_to_buffer(buffer, validation_result)
                        self.fuzzer_logger.log_state_validation_result(
                            validation_result, f"after operation {op_index + 1}"
                        )
                    
                    shard_results.append((op_index, 1 if success else 0, chaos_events, buffer, validation_result))
                    
                except Exception as e:
                    buffer.error(f"Operation failed with exception: {e}")
                    shard_results.append((op_index, 0, [], buffer, None))
            
            return shard_results
        
        # Execute shard groups in parallel
        with ThreadPoolExecutor(max_workers=len(shard_groups)) as executor:
            futures = {
                executor.submit(execute_shard_operations, shard_id, shard_ops): shard_id
                for shard_id, shard_ops in shard_groups.items()
            }
            
            # Collect results from all shards
            for future in as_completed(futures):
                try:
                    shard_results = future.result()
                    for op_index, executed, chaos_events, buffer, validation_result in shard_results:
                        results[op_index] = (executed, chaos_events, buffer, validation_result)
                except Exception as e:
                    logger.error(f"Shard execution failed: {e}")
        
        # Flush logs in operation order
        validation_results = []
        logger.info("")
        logger.info("Operation Logs")
        for result in results:
            if result:
                executed, chaos_events, buffer, validation_result = result
                operations_executed += executed
                all_chaos_events.extend(chaos_events)
                buffer.flush()
                if validation_result:
                    validation_results.append(validation_result)
        
        logger.info(f"Parallel execution complete: {operations_executed}/{len(operations)} succeeded")
        
        return operations_executed, all_chaos_events, validation_results
    
    def _add_validation_to_buffer(self, buffer: OperationLogBuffer, validation_result):
        """Add validation results to operation log buffer"""
        buffer.info(f"\n{'='*60}")
        buffer.info("VALIDATION RESULTS")
        buffer.info(f"{'='*60}")
        buffer.info(f"Overall: {'PASSED' if validation_result.overall_success else 'FAILED'}")
        
        if validation_result.slot_coverage:
            status = 'PASSED' if validation_result.slot_coverage.success else 'FAILED'
            buffer.info(f"  Slot Coverage: {status}")
        
        if validation_result.replication:
            status = 'PASSED' if validation_result.replication.success else 'FAILED'
            buffer.info(f"  Replication: {status}")
        
        if validation_result.data_consistency:
            status = 'PASSED' if validation_result.data_consistency.success else 'FAILED'
            buffer.info(f"  Data Consistency: {status}")
        
        if validation_result.cluster_status:
            status = 'PASSED' if validation_result.cluster_status.success else 'FAILED'
            buffer.info(f"  Cluster Status: {status}")
        
        buffer.info(f"{'='*60}\n")
